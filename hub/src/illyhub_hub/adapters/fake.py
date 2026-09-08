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
    vendor_label,
)
from ..tasks import stop_task
from .base import (
    ContentUnavailableError,
    DenonAdapter,
    HeosAdapter,
    PlayableTrack,
    PrimedQueue,
    ServiceAuthError,
    SonosAdapter,
    UnsupportedCommandError,
    VendorStation,
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


# Phase 5: canned Pandora stations per vendor (HUB_FAKE_PANDORA=1). Four names are shared so the
# merged list shows availability on both sides; one station per vendor is exclusive.
_SHARED_STATIONS = ("Chill Radio", "Jazz Nights", "90s Alternative", "Focus Flow")
FAKE_STATIONS = {
    "heos": [*_SHARED_STATIONS, "Living Room Mix"],
    "sonos": [*_SHARED_STATIONS, "Patio Party"],
}
STATION_ARTISTS = ("Low Tide", "Night Drive", "Analog Heart", "Static Bloom")
STATION_WORDS = ("Ember", "Drift", "Halo", "Lantern", "Meridian")


def fake_station(vendor: str, index: int, name: str) -> VendorStation:
    slug = name.casefold().replace(" ", "-")
    ids = (
        {"sid": "1", "cid": "my-stations", "mid": f"st-heos-{index + 1}"}
        if vendor == "heos"
        else {"id": f"ST:{10_000 + index}"}
    )
    return VendorStation(
        vendor=vendor, name=name, ids=ids, art_url=f"fake://art/station-{slug}", subtitle="Pandora"
    )


def station_track(station: VendorStation, n: int) -> FakeTrack:
    """The n-th track Pandora "picked" for a station: rotating names, the station as album."""
    return FakeTrack(
        f"{STATION_WORDS[n % len(STATION_WORDS)]} {n + 1}",
        STATION_ARTISTS[n % len(STATION_ARTISTS)],
        station.name,
        180_000 + (n * 11_000) % 60_000,
        source="pandora",
        art_url=station.art_url or "fake://art/station",
    )


DEFAULT_TRACK = FakeTrack("Signal", "Analog Heart", "Warm Glow", 213_000)
NEXT_TRACKS = (
    FakeTrack("Carrier", "Analog Heart", "Warm Glow", 187_000, art_url="fake://art/carrier"),
    FakeTrack("Sideband", "Analog Heart", "Warm Glow", 241_000, art_url="fake://art/sideband"),
    DEFAULT_TRACK,
)


class _Ticker:
    """Shared position ticker for fake sides in ``play`` state.

    Each side has its own clock: ``rate`` (1.0 = real time; 1.003 = 0.3 % fast) lets tests and the
    Playwright fake mode exercise the sync engine's drift correction, and ``seek_latency_s``
    delays a seek the way a real Sonos re-buffer does. ``on_end`` fires when a side's position
    passes the current track's duration so the fake queue advances like a real player.
    """

    def __init__(self, store: StateStore, interval_s: float) -> None:
        self.store = store
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._playing: dict[str, float] = {}
        self._paused: dict[str, float] = {}  # position kept while a side is not playing
        self.rates: dict[str, float] = {}
        self.seek_latency_s: dict[str, float] = {}
        self._pending_seeks: set[asyncio.Task[None]] = set()
        self.on_end: Callable[[str], None] | None = None
        # Like the real PositionTracker: the first report after a seek or track change is only
        # an optimistic estimate (confidence 0.5); the following ones are confirmed.
        self._fresh: dict[str, int] = {}

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="fake-ticker")

    async def stop(self) -> None:
        await stop_task(self._task)
        self._task = None
        for t in list(self._pending_seeks):
            await stop_task(t)
        self._pending_seeks.clear()

    def set_playing(self, side_id: str, playing: bool) -> None:
        if playing:
            self._playing.setdefault(side_id, self._paused.pop(side_id, 0))
        elif side_id in self._playing:
            self._paused[side_id] = self._playing.pop(side_id)

    def set_rate(self, side_id: str, rate: float) -> None:
        self.rates[side_id] = rate

    def set_seek_latency(self, side_id: str, seconds: float) -> None:
        self.seek_latency_s[side_id] = seconds

    def nudge(self, side_id: str, delta_ms: int) -> None:
        """Move a side's clock without a position report (a hidden drift, for scenarios)."""
        if side_id in self._playing:
            self._playing[side_id] += delta_ms
        else:
            self._paused[side_id] = self._paused.get(side_id, 0) + delta_ms

    def seek(self, side_id: str, position_ms: int) -> None:
        """Move the counter whether or not the side is playing; play resumes from here. Honours
        the side's configured seek latency (the counter keeps running until the seek lands)."""
        latency = self.seek_latency_s.get(side_id, 0.0)
        if latency <= 0:
            self._apply_seek(side_id, position_ms)
            return

        async def later() -> None:
            await asyncio.sleep(latency)
            self._apply_seek(side_id, position_ms)

        task = asyncio.get_running_loop().create_task(later())
        self._pending_seeks.add(task)
        task.add_done_callback(self._pending_seeks.discard)

    def _apply_seek(self, side_id: str, position_ms: int) -> None:
        if side_id in self._playing:
            self._playing[side_id] = position_ms
        else:
            self._paused[side_id] = position_ms
        self._fresh[side_id] = 1
        self.store.set_position(
            side_id, Position(position_ms=position_ms, reported_at=now(), confidence=0.5)
        )

    def position(self, side_id: str) -> int:
        return int(self._playing.get(side_id, self._paused.get(side_id, 0)))

    def tick(self) -> None:
        ended: list[str] = []
        for side_id in list(self._playing):
            rate = self.rates.get(side_id, 1.0)
            self._playing[side_id] += self.interval_s * 1000 * rate
            pos = int(self._playing[side_id])
            fresh = self._fresh.get(side_id, 0)
            if fresh:
                self._fresh[side_id] = fresh - 1
            self.store.set_position(
                side_id,
                Position(position_ms=pos, reported_at=now(), confidence=0.5 if fresh else 1.0),
            )
            np = self.store.state.now_playing.get(side_id)
            if np is not None and np.duration_ms and pos >= np.duration_ms:
                ended.append(side_id)
        for side_id in ended:
            if self.on_end is not None:
                self.on_end(side_id)

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
    _stations: dict[str, tuple[VendorStation, ContentRef, int]]  # side -> station, ref, track n
    pandora_enabled: bool = False  # HUB_FAKE_PANDORA: canned stations exist
    pandora_auth_fault: bool = False  # scenario: the vendor's Pandora session is broken

    def service_linked(self, service: str) -> bool:
        return service in self.linked_services

    # -- stations (Phase 5) -----------------------------------------------------------

    async def list_stations(self, service: str) -> list[VendorStation]:
        if not self.service_linked(service):
            raise ContentUnavailableError(
                f"{service} is not linked in the {vendor_label(self.vendor)} app"
            )
        if service == "pandora" and self.pandora_auth_fault:
            raise ServiceAuthError(
                f"Pandora on {vendor_label(self.vendor)} needs to be signed in again in the "
                f"{vendor_label(self.vendor)} app"
            )
        if service != "pandora" or not self.pandora_enabled:
            return []
        return [fake_station(self.vendor, i, n) for i, n in enumerate(FAKE_STATIONS[self.vendor])]

    async def play_station(self, player_id: str, ref: ContentRef, station: VendorStation) -> None:
        self._check(player_id)
        if not self.service_linked(ref.service):
            raise ContentUnavailableError(
                f"{ref.service} is not linked in the {vendor_label(self.vendor)} app"
            )
        side = self._side_for(player_id)
        self._queues.pop(side, None)
        self._stations[side] = (station, ref, 0)
        self.change_track(player_id, station_track(station, 0), content_ref=ref)
        self.sim_play_state(player_id, "play")

    def _station_advance(self, player_id: str) -> bool:
        side = self._side_for(player_id)
        current = self._stations.get(side)
        if current is None:
            return False
        station, ref, n = current
        self._stations[side] = (station, ref, n + 1)
        self.change_track(player_id, station_track(station, n + 1), content_ref=ref)
        return True

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
            raise ContentUnavailableError(
                f"{ref.service} is not linked in the {vendor_label(self.vendor)} app"
            )
        if not 0 <= start_index < len(tracks):
            raise IndexError(f"start_index {start_index} out of range for {len(tracks)} tracks")
        side = self._side_for(player_id)
        self._stations.pop(side, None)
        self._queues[side] = (list(tracks), start_index)
        self.change_track(
            player_id,
            self._track_from_playable(tracks[start_index]),
            track_id=tracks[start_index].track_id,
        )
        self.sim_play_state(player_id, "play")

    async def prime_content(
        self, player_id: str, ref: ContentRef, tracks: list[PlayableTrack], start_index: int = 0
    ) -> PrimedQueue:
        """Load the queue positioned at ``start_index`` and leave the side paused at 0."""
        self._check(player_id)
        if not self.service_linked(ref.service):
            raise ContentUnavailableError(
                f"{ref.service} is not linked in the {vendor_label(self.vendor)} app"
            )
        if not 0 <= start_index < len(tracks):
            raise IndexError(f"start_index {start_index} out of range for {len(tracks)} tracks")
        side = self._side_for(player_id)
        self._stations.pop(side, None)
        self._queues[side] = (list(tracks), start_index)
        self.sim_play_state(player_id, "pause")
        track = tracks[start_index]
        self.change_track(player_id, self._track_from_playable(track), track_id=track.track_id)
        return PrimedQueue(track_id=track.track_id, title=track.title)

    async def start_primed(self, player_id: str, start_index: int = 0) -> None:
        self._check(player_id)
        self.sim_play_state(player_id, "play")

    def queue_index(self, player_id: str) -> int | None:
        queued = self._queues.get(self._side_for(player_id))
        return queued[1] if queued else None

    def end_of_track(self, side_id: str) -> None:
        """Ticker callback: the current track ran out; advance the queue like a real player."""
        side = self.store.state.sides.get(side_id)
        if side is None or side.vendor != self.vendor:
            return
        self._advance_queue(side.coordinator_player_id, +1)

    def _advance_queue(self, player_id: str, step: int) -> bool:
        side = self._side_for(player_id)
        if side in self._stations:
            if step < 0:
                raise UnsupportedCommandError("stations cannot go back")
            return self._station_advance(player_id)
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
        self.change_track(
            player_id, self._track_from_playable(tracks[nxt]), track_id=tracks[nxt].track_id
        )
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

    def vendor_track_id(self, canonical_id: str, service: str = "tidal") -> str:
        """Vendor-native id for a canonical service track id, matching the real adapters' shapes
        (HEOS: bare media id; Sonos: the track URI). Overridden per fake."""
        return canonical_id

    def change_track(
        self,
        player_id: str,
        track: FakeTrack = DEFAULT_TRACK,
        *,
        track_id: str | None = None,
        content_ref: ContentRef | None = None,
    ) -> None:
        """``track_id`` is the *canonical* service id; the fake stamps its vendor-native form on
        ``track_id`` and the canonical one on ``content_ref``, like the real adapters do.
        ``content_ref`` overrides that derivation (stations: the vendor-neutral station ref)."""
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
                track_id=self.vendor_track_id(track_id, track.source) if track_id else None,
                content_ref=content_ref
                or (
                    ContentRef(service=track.source, kind="track", id=track_id)  # type: ignore[arg-type]
                    if track_id and track.source in ("tidal", "ytmusic")
                    else None
                ),
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

    def vendor_track_id(self, canonical_id: str, service: str = "tidal") -> str:
        return canonical_id  # HEOS media_id is the bare service id

    def __init__(self, store: StateStore, ticker: _Ticker, art: ArtHelper | None = None) -> None:
        super().__init__(store, art=art)
        self._ticker = ticker
        self._connected = False
        self.linked_services = {"tidal", "pandora"}
        self._queues = {}
        self._stations = {}

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

    def vendor_track_id(self, canonical_id: str, service: str = "tidal") -> str:
        if service == "ytmusic":
            return f"x-sonos-http:sonos-track:{canonical_id}.mp4?sid=284&flags=8232&sn=1"
        return f"x-sonos-http:track/{canonical_id}.flac?sid=174&flags=8224&sn=1"

    async def snapshot_queue(self, player_id: str) -> dict[str, object] | None:
        side = self._side_for(player_id)
        queued = self._queues.get(side)
        np = self.store.state.now_playing.get(side)
        return {
            "queue": (list(queued[0]), queued[1]) if queued else None,
            "now_playing": np,
            "play_state": self.store.state.players[player_id].play_state,
            "position_ms": self._ticker.position(side),
        }

    async def restore_queue(self, player_id: str, snapshot: object) -> None:
        assert isinstance(snapshot, dict)
        side = self._side_for(player_id)
        self.restored_queues = getattr(self, "restored_queues", 0) + 1
        if snapshot["queue"] is not None:
            self._queues[side] = snapshot["queue"]  # type: ignore[assignment]
        else:
            self._queues.pop(side, None)
        np = snapshot["now_playing"]
        if np is not None:
            self.store.set_now_playing(side, np)  # type: ignore[arg-type]
        self.sim_play_state(player_id, snapshot["play_state"])  # type: ignore[arg-type]
        self._ticker.seek(side, int(snapshot["position_ms"]))  # type: ignore[arg-type]

    def __init__(self, store: StateStore, ticker: _Ticker, art: ArtHelper | None = None) -> None:
        super().__init__(store, art=art)
        self._ticker = ticker
        self._connected = False
        self.linked_services = {"tidal", "pandora", "ytmusic"}
        self._queues = {}
        self._stations = {}

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

    def _resolve(self, zone_id: str) -> Zone:
        zone = self.store.state.zones.get(zone_id) or self.store.state.zones.get(
            fake_zone_id(zone_id)
        )
        if zone is None:
            raise ValueError(f"unknown zone {zone_id!r}")
        return zone

    async def set_power(self, zone_id: str, on: bool) -> None:
        zone = self._resolve(zone_id)
        self.store.set_zone(zone.model_copy(update={"power": on}))
        self._emit("zone_power", zone_id=zone.id, power=on)

    async def set_volume(self, zone_id: str, level: int) -> None:
        zone = self._resolve(zone_id)
        self.store.set_zone(zone.model_copy(update={"volume": max(0, min(level, 100))}))
        self._emit("zone_volume", zone_id=zone.id, level=level)

    async def set_mute(self, zone_id: str, on: bool) -> None:
        zone = self._resolve(zone_id)
        self.store.set_zone(zone.model_copy(update={"muted": on}))
        self._emit("zone_mute", zone_id=zone.id, muted=on)


class FakeBundle:
    """The three fakes plus their shared ticker, wired to one store."""

    def __init__(
        self, store: StateStore, tick_interval_s: float = 1.0, art: ArtHelper | None = None
    ) -> None:
        self.store = store
        self.ticker = _Ticker(store, tick_interval_s)
        self.heos = FakeHeos(store, self.ticker, art)
        self.sonos = FakeSonos(store, self.ticker, art)
        self.denon = FakeDenon(store)
        self.ticker.on_end = self._track_ended
        self.tidal_linked = True
        self.tidal_pending = False  # fake device-code flow in progress
        self.on_tidal_link: Callable[[bool], None] | None = None  # flips the fake catalog
        self.ytmusic_linked = True
        self.ytmusic_pending = False
        self.on_ytmusic_link: Callable[[bool], None] | None = None
        self._ytmusic_link_task: asyncio.Task[None] | None = None
        self.on_pandora_link: Callable[[], None] | None = None  # drops the station cache
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

    def set_ytmusic_linked(self, linked: bool) -> None:
        """Simulate linking/unlinking YouTube Music: the hub account (catalog) and the Sonos app.
        HEOS never gains it (no YouTube Music source on HEOS, PRD §3.1)."""
        self.ytmusic_linked = linked
        self.ytmusic_pending = False
        if linked:
            self.sonos.linked_services.add("ytmusic")
        else:
            self.sonos.linked_services.discard("ytmusic")
        self.heos.linked_services.discard("ytmusic")
        if self.on_ytmusic_link is not None:
            self.on_ytmusic_link(linked)

    def start_fake_ytmusic_link(self) -> None:
        self.ytmusic_pending = True
        if self._ytmusic_link_task is not None:
            self._ytmusic_link_task.cancel()

        async def approve_later() -> None:
            await asyncio.sleep(self.fake_link_delay_s)
            if self.ytmusic_pending:
                self.set_ytmusic_linked(True)

        self._ytmusic_link_task = asyncio.get_running_loop().create_task(approve_later())

    def approve_fake_ytmusic_link(self) -> bool:
        if not self.ytmusic_pending:
            return False
        if self._ytmusic_link_task is not None:
            self._ytmusic_link_task.cancel()
            self._ytmusic_link_task = None
        self.set_ytmusic_linked(True)
        return True

    @property
    def ytmusic_state(self) -> str:
        if self.ytmusic_linked:
            return "linked"
        return "pending" if self.ytmusic_pending else "unlinked"

    @property
    def pandora_enabled(self) -> bool:
        return self.heos.pandora_enabled

    @pandora_enabled.setter
    def pandora_enabled(self, enabled: bool) -> None:
        self.heos.pandora_enabled = enabled
        self.sonos.pandora_enabled = enabled

    def set_pandora_linked(self, vendor: str | None, linked: bool) -> dict[str, bool]:
        """Simulate linking Pandora inside one vendor app (or both when ``vendor`` is None).
        Pandora is per-ecosystem (PRD §3.5): there is no hub account to link."""
        targets = [self.heos, self.sonos] if vendor is None else [getattr(self, vendor)]
        for a in targets:
            if linked:
                a.linked_services.add("pandora")
                a.pandora_auth_fault = False
            else:
                a.linked_services.discard("pandora")
        if self.on_pandora_link is not None:
            self.on_pandora_link()
        return {a.vendor: a.service_linked("pandora") for a in (self.heos, self.sonos)}

    def set_pandora_auth_fault(self, vendor: str | None, broken: bool) -> dict[str, bool]:
        """Simulate an expired Pandora session inside a vendor app: linked, but browsing faults."""
        targets = [self.heos, self.sonos] if vendor is None else [getattr(self, vendor)]
        for a in targets:
            a.pandora_auth_fault = broken
        if self.on_pandora_link is not None:
            self.on_pandora_link()
        return {a.vendor: a.pandora_auth_fault for a in (self.heos, self.sonos)}

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

    def _track_ended(self, side_id: str) -> None:
        for a in (self.heos, self.sonos):
            a.end_of_track(side_id)

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
        if self._ytmusic_link_task is not None:
            self._ytmusic_link_task.cancel()
            self._ytmusic_link_task = None
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
        "link_ytmusic",
        "unlink_ytmusic",
        "approve_ytmusic",
        "sync_drift",
        "sync_lose_sonos",
        "sync_track_change",
        "link_pandora",
        "unlink_pandora",
        "pandora_auth_fault",
        "pandora_auth_ok",
        "station_track_change",
    )

    def run_scenario(self, name: str, *, vendor: str | None = None) -> dict[str, object]:
        """Simulate a change made outside the hub (vendor app, cable pull). Returns a summary.
        ``vendor`` (``heos`` | ``sonos``) scopes the Pandora scenarios to one ecosystem.
        Unknown ``name`` → ``KeyError`` (404); bad ``vendor`` → ``ValueError`` (400)."""
        if name not in self.SCENARIOS:
            raise KeyError(name)
        if vendor is not None and vendor not in ("heos", "sonos"):
            raise ValueError(vendor)
        if name == "pandora_auth_fault":
            return {"pandora_auth_fault": self.set_pandora_auth_fault(vendor, True)}
        if name == "pandora_auth_ok":
            return {"pandora_auth_fault": self.set_pandora_auth_fault(vendor, False)}
        if name == "link_pandora":
            return {"pandora_linked": self.set_pandora_linked(vendor, True)}
        if name == "unlink_pandora":
            return {"pandora_linked": self.set_pandora_linked(vendor, False)}
        if name == "station_track_change":
            advanced = [
                a.vendor
                for a in (self.heos, self.sonos)
                for side, (_st, _ref, _n) in list(a._stations.items())
                if a._station_advance(self.store.state.sides[side].coordinator_player_id)
            ]
            return {"advanced": advanced}
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
        if name == "link_ytmusic":
            self.set_ytmusic_linked(True)
            return {"ytmusic_linked": True}
        if name == "unlink_ytmusic":
            self.set_ytmusic_linked(False)
            return {"ytmusic_linked": False}
        if name == "approve_ytmusic":
            approved = self.approve_fake_ytmusic_link()
            return {"ytmusic_linked": self.ytmusic_linked, "approved": approved}
        if name == "sync_drift":
            side = self.sonos.side_id()
            self.ticker.nudge(side, 800)
            return {"side": side, "nudged_ms": 800}
        if name == "sync_lose_sonos":
            coord = self.sonos.store.state.sides[self.sonos.side_id()].coordinator_player_id
            self.sonos.sim_play_state(coord, "pause")
            return {"player": coord, "play_state": "pause"}
        if name == "sync_track_change":
            advanced = self.heos._advance_queue(HEOS_PLAYER, +1)
            np = self.heos.store.state.now_playing.get(side_id_for_group("heos", HEOS_PLAYER))
            title = np.title if np else None
            return {"player": HEOS_PLAYER, "advanced": advanced, "title": title}
        raise KeyError(name)
