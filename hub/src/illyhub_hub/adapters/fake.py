"""Protocol fakes for offline development and tests.

Selected with ``HUB_FAKE_DEVICES=1``. Seeds one HEOS player behind a Denon with two zones and
two Sonos players grouped under Kitchen. Emits 1 Hz position ticks while a side is playing.

Two surfaces:
- the :class:`~illyhub_hub.adapters.base.PlaybackAdapter` command methods (``play``,
  ``set_volume``, ``set_group`` ...) which behave like the real devices would: they mutate fake
  state and emit the same events the real adapters emit;
- synchronous scripting methods (``sim_volume``, ``sim_play_state``, ``change_track``, ``drop``,
  ``restore``, ``group`` ...) that simulate changes made from the vendor apps, so tests and the
  Playwright suite can drive scenarios via ``POST /api/dev/fake/{scenario}``.

The fake HEOS refuses ``seek`` exactly like the real CLI does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from ..art import ArtHelper, ArtHints
from ..content import ContentRef
from ..state import (
    Capabilities,
    GroupTopology,
    NowPlaying,
    Player,
    PlayState,
    Position,
    StateStore,
    Zone,
    now,
    side_id_for,
    side_id_for_group,
)
from ..tasks import stop_task
from .base import (
    ContentUnavailableError,
    DenonAdapter,
    HeosAdapter,
    PlayableTrack,
    SonosAdapter,
    UnsupportedCommandError,
)

FAKE_DENON_HOST = "fake"
# Ids follow the real adapters' shapes: heos-<pid>, heos-g<gid>, sonos-<uid>, sonos-g<group uid>.
HEOS_PLAYER = "heos-1"
HEOS_PLAYER_2 = "heos-2"
KITCHEN_UID = "RINCON_KITCHEN"
PATIO_UID = "RINCON_PATIO"
KITCHEN = f"sonos-{KITCHEN_UID}"
PATIO = f"sonos-{PATIO_UID}"


def fake_zone_id(key: str) -> str:
    return f"denon-{FAKE_DENON_HOST}:{key}"


@dataclass(frozen=True)
class FakeTrack:
    title: str
    artist: str
    album: str
    duration_ms: int
    source: str = "tidal"
    art_url: str = "fake://art/signal"  # the art proxy renders synthetic art for fake:// URLs


DEFAULT_TRACK = FakeTrack("Signal", "Analog Heart", "Warm Glow", 213_000)
NEXT_TRACKS = (
    FakeTrack("Carrier", "Analog Heart", "Warm Glow", 187_000, art_url="fake://art/carrier"),
    FakeTrack("Sideband", "Analog Heart", "Warm Glow", 241_000, art_url="fake://art/sideband"),
    DEFAULT_TRACK,
)


class _Ticker:
    """Shared 1 Hz position ticker for fake sides in ``play`` state."""

    def __init__(self, store: StateStore, interval_s: float) -> None:
        self.store = store
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._playing: dict[str, int] = {}
        self._paused: dict[str, int] = {}  # position kept while a side is not playing

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="fake-ticker")

    async def stop(self) -> None:
        await stop_task(self._task)
        self._task = None

    def set_playing(self, side_id: str, playing: bool) -> None:
        if playing:
            self._playing.setdefault(side_id, self._paused.pop(side_id, 0))
        elif side_id in self._playing:
            self._paused[side_id] = self._playing.pop(side_id)

    def seek(self, side_id: str, position_ms: int) -> None:
        """Move the counter whether or not the side is playing; play resumes from here."""
        if side_id in self._playing:
            self._playing[side_id] = position_ms
        else:
            self._paused[side_id] = position_ms
        self.store.set_position(
            side_id, Position(position_ms=position_ms, reported_at=now(), confidence=1.0)
        )

    def position(self, side_id: str) -> int:
        return self._playing.get(side_id, self._paused.get(side_id, 0))

    def tick(self) -> None:
        for side_id in list(self._playing):
            self._playing[side_id] += int(self.interval_s * 1000)
            self.store.set_position(
                side_id,
                Position(position_ms=self._playing[side_id], reported_at=now(), confidence=1.0),
            )

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            self.tick()


class FakeVendorMixin:
    """Scripting surface plus PlaybackAdapter commands shared by the HEOS and Sonos fakes."""

    # Attributes below are supplied by BaseAdapter / the concrete fake; annotated for typing.
    store: StateStore
    vendor: str
    _ticker: _Ticker
    _connected: bool
    _emit: Callable[..., None]
    _set_status: Callable[..., None]

    def _side_for(self, player_id: str) -> str:
        return side_id_for(self.store.state.players[player_id])

    # -- content (Phase 3): a fake queue per side ------------------------------------

    linked_services: set[str]
    _queues: dict[str, tuple[list[PlayableTrack], int]]

    def service_linked(self, service: str) -> bool:
        return service in self.linked_services

    def _track_from_playable(self, t: PlayableTrack) -> FakeTrack:
        return FakeTrack(
            t.title,
            t.artist or "",
            t.album or "",
            t.duration_ms or 200_000,
            source=t.service,
            art_url=f"fake://art/{(t.album_id or t.track_id).replace(':', '-')}",
        )

    async def play_content(
        self, player_id: str, ref: ContentRef, tracks: list[PlayableTrack], start_index: int = 0
    ) -> None:
        self._check(player_id)
        if not self.service_linked(ref.service):
            raise ContentUnavailableError(f"{ref.service} is not linked in the {self.vendor} app")
        if not 0 <= start_index < len(tracks):
            raise IndexError(f"start_index {start_index} out of range for {len(tracks)} tracks")
        side = self._side_for(player_id)
        self._queues[side] = (list(tracks), start_index)
        self.change_track(player_id, self._track_from_playable(tracks[start_index]))
        self.sim_play_state(player_id, "play")

    def _advance_queue(self, player_id: str, step: int) -> bool:
        side = self._side_for(player_id)
        queued = self._queues.get(side)
        if not queued:
            return False
        tracks, idx = queued
        nxt = idx + step
        if not 0 <= nxt < len(tracks):
            # End of queue: like the real players, stop instead of wrapping around.
            if step > 0:
                self.sim_play_state(player_id, "stop")
                self._ticker.seek(side, 0)
            else:
                self._ticker.seek(side, 0)  # "previous" at the first track restarts it
            return True
        self._queues[side] = (tracks, nxt)
        self.change_track(player_id, self._track_from_playable(tracks[nxt]))
        return True

    # -- scripting (simulates the vendor app) -----------------------------------------

    def sim_volume(self, player_id: str, volume: int, muted: bool | None = None) -> None:
        fields: dict[str, object] = {"volume": volume}
        if muted is not None:
            fields["muted"] = muted
        self.store.update_player(player_id, **fields)
        self._emit("volume", player_id=player_id)

    def sim_play_state(self, player_id: str, state: PlayState) -> None:
        side = self._side_for(player_id)
        for member in self.store.state.sides[side].member_ids:
            self.store.update_player(member, play_state=state)
        self._ticker.set_playing(side, state == "play")
        self._emit("play_state", player_id=player_id)

    def change_track(self, player_id: str, track: FakeTrack = DEFAULT_TRACK) -> None:
        side = self._side_for(player_id)
        self.store.set_now_playing(
            side,
            NowPlaying(
                title=track.title,
                artist=track.artist,
                album=track.album,
                art=self.art.ref(ArtHints(device_url=track.art_url, service=track.source)),
                source=track.source,
                seekable=track.source != "pandora",
                supports_next=True,  # stations skip forward on both ecosystems
                supports_prev=track.source != "pandora",
                duration_ms=track.duration_ms,
            ),
        )
        self._ticker.seek(side, 0)
        self._ticker.set_playing(side, self.store.state.players[player_id].play_state == "play")
        self._emit("now_playing", player_id=player_id)

    def drop(self, error: str = "simulated disconnect") -> None:
        self._connected = False
        self.store.mark_vendor_offline(self.vendor)  # type: ignore[arg-type]
        self._set_status("reconnecting", error)
        self._emit("disconnected")

    def restore(self) -> None:
        self._connected = True
        for pid, p in self.store.state.players.items():
            if p.vendor == self.vendor:
                self.store.update_player(pid, online=True)
        self._set_status("connected")
        self._emit("connected")

    # -- PlaybackAdapter commands (behave like the device) ---------------------------

    def _check(self, player_id: str) -> None:
        if not self._connected:
            raise ConnectionError(f"{self.vendor} fake is disconnected")
        if player_id not in self.store.state.players:
            raise ConnectionError(f"unknown player {player_id!r}")

    async def play(self, player_id: str) -> None:
        self._check(player_id)
        self.sim_play_state(player_id, "play")

    async def pause(self, player_id: str) -> None:
        self._check(player_id)
        self.sim_play_state(player_id, "pause")

    async def stop(self, player_id: str) -> None:
        self._check(player_id)
        self.sim_play_state(player_id, "stop")
        self._ticker.seek(self._side_for(player_id), 0)

    async def next(self, player_id: str) -> None:
        self._check(player_id)
        if self._advance_queue(player_id, +1):
            return
        current = self.store.state.now_playing.get(self._side_for(player_id))
        titles = [t.title for t in NEXT_TRACKS]
        idx = titles.index(current.title) if current and current.title in titles else -1
        self.change_track(player_id, NEXT_TRACKS[(idx + 1) % len(NEXT_TRACKS)])

    async def previous(self, player_id: str) -> None:
        self._check(player_id)
        if self._advance_queue(player_id, -1):
            return
        self._ticker.seek(self._side_for(player_id), 0)

    async def seek(self, player_id: str, position_ms: int) -> None:
        self._check(player_id)
        self._ticker.seek(self._side_for(player_id), position_ms)

    async def set_volume(self, player_id: str, level: int) -> None:
        self._check(player_id)
        self.sim_volume(player_id, level)

    async def set_mute(self, player_id: str, muted: bool) -> None:
        self._check(player_id)
        self.store.update_player(player_id, muted=muted)
        self._emit("volume", player_id=player_id)


class FakeHeos(FakeVendorMixin, HeosAdapter):
    vendor = "heos"

    def __init__(self, store: StateStore, ticker: _Ticker, art: ArtHelper | None = None) -> None:
        super().__init__(store, art=art)
        self._ticker = ticker
        self._connected = False
        self.linked_services = {"tidal", "pandora"}
        self._queues = {}

    async def connect(self) -> None:
        if self._connected:
            return
        self.store.upsert_player(
            Player(
                id=HEOS_PLAYER,
                name="Living Room Amp",
                vendor="heos",
                ip="192.168.1.20",
                model="Denon AVR-X3700H",
                volume=35,
                capabilities=Capabilities(supports_power=True, supports_seek=False),
            )
        )
        self.store.upsert_player(
            Player(
                id=HEOS_PLAYER_2,
                name="Den",
                vendor="heos",
                ip="192.168.1.21",
                model="HEOS 1",
                volume=12,
                capabilities=Capabilities(supports_power=False, supports_seek=False),
            )
        )
        self.store.set_groups("heos", [])
        self._connected = True
        self._set_status("connected")
        self.change_track(HEOS_PLAYER)
        self._emit("connected", host="fake")

    async def disconnect(self) -> None:
        self._connected = False
        self._set_status("disconnected")

    async def seek(self, player_id: str, position_ms: int) -> None:
        raise UnsupportedCommandError("the HEOS CLI protocol has no seek command")

    _next_gid = 1

    def _heos_groups(self) -> list[GroupTopology]:
        return [g for g in self.store.state.groups.values() if g.vendor == "heos"]

    async def set_group(self, member_ids: list[str]) -> None:
        """Mirror ``group/set_group``: the list *is* the new group. A single id removes that
        player from its group; if it led the group, the group dissolves (HEOS semantics)."""
        for m in member_ids:
            self._check(m)
        lead = member_ids[0]
        groups = self._heos_groups()
        if len(member_ids) == 1:
            kept: list[GroupTopology] = []
            for g in groups:
                if g.coordinator_player_id == lead:
                    continue  # leader leaving dissolves the group
                if lead in g.member_ids:
                    g = g.model_copy(update={"member_ids": [x for x in g.member_ids if x != lead]})
                if len(g.member_ids) > 1:
                    kept.append(g)
            self.store.set_groups("heos", kept)
        else:
            others = [
                g.model_copy(
                    update={"member_ids": [x for x in g.member_ids if x not in member_ids]}
                )
                for g in groups
                if g.coordinator_player_id not in member_ids
            ]
            existing = next((g for g in groups if g.coordinator_player_id == lead), None)
            gid = existing.id if existing else f"heos-g{FakeHeos._next_gid}"
            if existing is None:
                FakeHeos._next_gid += 1
            new = GroupTopology(
                id=gid, vendor="heos", coordinator_player_id=lead, member_ids=list(member_ids)
            )
            self.store.set_groups("heos", [g for g in others if len(g.member_ids) > 1] + [new])
        self._emit("topology")

    async def dissolve(self, coordinator_id: str, member_ids: list[str]) -> None:
        await self.set_group([coordinator_id])


class FakeSonos(FakeVendorMixin, SonosAdapter):
    vendor = "sonos"

    def __init__(self, store: StateStore, ticker: _Ticker, art: ArtHelper | None = None) -> None:
        super().__init__(store, art=art)
        self._ticker = ticker
        self._connected = False
        self.linked_services = {"tidal", "pandora", "ytmusic"}
        self._queues = {}

    async def connect(self) -> None:
        if self._connected:
            return
        for pid, name, ip in (
            (KITCHEN, "Kitchen", "192.168.1.31"),
            (PATIO, "Patio", "192.168.1.32"),
        ):
            self.store.upsert_player(
                Player(id=pid, name=name, vendor="sonos", ip=ip, model="Sonos One", volume=20)
            )
        self.group([KITCHEN, PATIO], coordinator=KITCHEN)
        self._connected = True
        self._set_status("connected")
        self._emit("connected", players=2)

    async def disconnect(self) -> None:
        self._connected = False
        self._set_status("disconnected")

    def _sonos_groups(self) -> list[GroupTopology]:
        return [g for g in self.store.state.groups.values() if g.vendor == "sonos"]

    def _unjoin(self, player_id_: str) -> list[GroupTopology]:
        """Like ``SoCo.unjoin``: the player leaves its group; a coordinator leaving hands the
        group to the next member."""
        result: list[GroupTopology] = []
        for g in self._sonos_groups():
            if player_id_ not in g.member_ids:
                result.append(g)
                continue
            members = [x for x in g.member_ids if x != player_id_]
            if len(members) < 2:
                continue
            coord = g.coordinator_player_id if g.coordinator_player_id in members else members[0]
            result.append(
                g.model_copy(update={"member_ids": members, "coordinator_player_id": coord})
            )
        return result

    async def set_group(self, member_ids: list[str]) -> None:
        """Reconcile like the real adapter: leavers unjoin, newcomers join the coordinator."""
        for m in member_ids:
            self._check(m)
        coordinator = member_ids[0]
        if len(member_ids) == 1:
            self.store.set_groups("sonos", self._unjoin(coordinator))
            self._emit("topology")
            return
        groups = self._sonos_groups()
        current = next((g for g in groups if coordinator in g.member_ids), None)
        current_members = current.member_ids if current else [coordinator]
        for leaver in current_members:
            if leaver not in member_ids:
                self.store.set_groups("sonos", self._unjoin(leaver))
        groups = self._sonos_groups()
        for g in groups:
            for newcomer in member_ids[1:]:
                if newcomer in g.member_ids and coordinator not in g.member_ids:
                    self.store.set_groups("sonos", self._unjoin(newcomer))
        groups = [g for g in self._sonos_groups() if coordinator not in g.member_ids]
        existing = next((g for g in self._sonos_groups() if coordinator in g.member_ids), None)
        gid = existing.id if existing else self.group_id(coordinator)
        new = GroupTopology(
            id=gid, vendor="sonos", coordinator_player_id=coordinator, member_ids=list(member_ids)
        )
        self.store.set_groups("sonos", groups + [new])
        self._emit("topology")

    async def dissolve(self, coordinator_id: str, member_ids: list[str]) -> None:
        for m in member_ids:
            if m != coordinator_id:
                self.store.set_groups("sonos", self._unjoin(m))
        self._emit("topology")

    @staticmethod
    def group_id(coordinator: str) -> str:
        """Real Sonos group uids look like ``RINCON_…:12``; the hub prefixes ``sonos-g``."""
        return f"sonos-g{coordinator.removeprefix('sonos-')}:1"

    def group(self, member_ids: list[str], coordinator: str) -> None:
        topo = [
            GroupTopology(
                id=self.group_id(coordinator),
                vendor="sonos",
                coordinator_player_id=coordinator,
                member_ids=member_ids,
            )
        ]
        self.store.set_groups("sonos", topo if len(member_ids) > 1 else [])
        self._emit("topology")

    def ungroup_all(self) -> None:
        self.store.set_groups("sonos", [])
        self._emit("topology")

    def side_id(self, coordinator: str = KITCHEN) -> str:
        p = self.store.state.players[coordinator]
        return side_id_for(p) if p.group_id else side_id_for_group("sonos", coordinator)


class FakeDenon(DenonAdapter):
    def __init__(self, store: StateStore) -> None:
        super().__init__(store)
        self._connected = False

    async def connect(self) -> None:
        if self._connected:
            return
        for key, name, power in (("main", "Main zone", True), ("zone2", "Zone 2", False)):
            self.store.set_zone(
                Zone(
                    id=fake_zone_id(key),
                    key=key,
                    name=name,
                    power=power,
                    host=FAKE_DENON_HOST,
                    device_id=f"denon-{FAKE_DENON_HOST}",
                    player_ids=[HEOS_PLAYER],
                )
            )
        self._connected = True
        self._set_status("connected")

    async def disconnect(self) -> None:
        self._connected = False
        self._set_status("disconnected")

    async def set_power(self, zone_id: str, on: bool) -> None:
        zone = self.store.state.zones.get(zone_id) or self.store.state.zones.get(
            fake_zone_id(zone_id)
        )
        if zone is None:
            raise ValueError(f"unknown zone {zone_id!r}")
        self.store.set_zone(zone.model_copy(update={"power": on}))
        self._emit("zone_power", zone_id=zone.id, power=on)


class FakeBundle:
    """The three fakes plus their shared ticker, wired to one store."""

    def __init__(
        self, store: StateStore, tick_interval_s: float = 1.0, art: ArtHelper | None = None
    ) -> None:
        self.ticker = _Ticker(store, tick_interval_s)
        self.heos = FakeHeos(store, self.ticker, art)
        self.sonos = FakeSonos(store, self.ticker, art)
        self.denon = FakeDenon(store)
        self.tidal_linked = True
        self.tidal_pending = False  # fake device-code flow in progress
        self.on_tidal_link: Callable[[bool], None] | None = None  # flips the fake catalog
        self._link_task: asyncio.Task[None] | None = None
        self.fake_link_delay_s = 1.0

    def set_tidal_linked(self, linked: bool) -> None:
        """Simulate linking/unlinking Tidal everywhere at once: the hub account (catalog) and
        both vendor apps (per-side availability)."""
        self.tidal_linked = linked
        self.tidal_pending = False
        for a in (self.heos, self.sonos):
            if linked:
                a.linked_services.add("tidal")
            else:
                a.linked_services.discard("tidal")
        if self.on_tidal_link is not None:
            self.on_tidal_link(linked)

    def start_fake_link(self) -> None:
        """Mimic the device-code flow: pending for ``fake_link_delay_s``, then linked. The app
        can render the pending UI; ``approve_tidal`` completes it immediately."""
        self.tidal_pending = True
        if self._link_task is not None:
            self._link_task.cancel()

        async def approve_later() -> None:
            await asyncio.sleep(self.fake_link_delay_s)
            if self.tidal_pending:
                self.set_tidal_linked(True)

        self._link_task = asyncio.get_running_loop().create_task(approve_later())

    def approve_fake_link(self) -> bool:
        if not self.tidal_pending:
            return False
        if self._link_task is not None:
            self._link_task.cancel()
            self._link_task = None
        self.set_tidal_linked(True)
        return True

    @property
    def tidal_state(self) -> str:
        if self.tidal_linked:
            return "linked"
        return "pending" if self.tidal_pending else "unlinked"

    @property
    def adapters(self) -> list[HeosAdapter | SonosAdapter | DenonAdapter]:
        return [self.heos, self.sonos, self.denon]

    async def start(self) -> None:
        for a in self.adapters:
            await a.connect()
        self.ticker.start()

    async def stop(self) -> None:
        await self.ticker.stop()
        if self._link_task is not None:
            self._link_task.cancel()
            self._link_task = None
        for a in self.adapters:
            await a.disconnect()

    # -- scripted scenarios for /api/dev/fake/{scenario} --------------------------------

    SCENARIOS = (
        "track_change",
        "volume_change",
        "disconnect_heos",
        "reconnect_heos",
        "sonos_regroup",
        "link_tidal",
        "unlink_tidal",
        "approve_tidal",
    )

    def run_scenario(self, name: str) -> dict[str, object]:
        """Simulate a change made outside the hub (vendor app, cable pull). Returns a summary."""
        if name == "track_change":
            current = self.heos.store.state.now_playing.get(side_id_for_group("heos", HEOS_PLAYER))
            titles = [t.title for t in NEXT_TRACKS]
            idx = titles.index(current.title) if current and current.title in titles else -1
            track = NEXT_TRACKS[(idx + 1) % len(NEXT_TRACKS)]
            self.heos.change_track(HEOS_PLAYER, track)
            return {"player": HEOS_PLAYER, "title": track.title}
        if name == "volume_change":
            vol = (self.sonos.store.state.players[PATIO].volume + 10) % 100
            self.sonos.sim_volume(PATIO, vol)
            return {"player": PATIO, "volume": vol}
        if name == "disconnect_heos":
            self.heos.drop("simulated cable pull")
            return {"adapter": "heos", "state": "reconnecting"}
        if name == "reconnect_heos":
            self.heos.restore()
            return {"adapter": "heos", "state": "connected"}
        if name == "sonos_regroup":
            grouped = self.sonos.store.state.players[PATIO].group_id is not None
            if grouped:
                self.sonos.ungroup_all()
            else:
                self.sonos.group([KITCHEN, PATIO], coordinator=KITCHEN)
            return {"grouped": not grouped}
        if name == "link_tidal":
            self.set_tidal_linked(True)
            return {"tidal_linked": True}
        if name == "unlink_tidal":
            self.set_tidal_linked(False)
            return {"tidal_linked": False}
        if name == "approve_tidal":
            approved = self.approve_fake_link()
            return {"tidal_linked": self.tidal_linked, "approved": approved}
        raise KeyError(name)
