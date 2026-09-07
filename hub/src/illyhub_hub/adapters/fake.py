"""Protocol fakes for offline development and tests.

Selected with ``HUB_FAKE_DEVICES=1``. Seeds one HEOS player behind a Denon with two zones and
two Sonos players grouped under Kitchen. Emits 1 Hz position ticks while a side is playing, and
exposes scripting methods (``set_volume``, ``change_track``, ``drop``, ``restore`` ...) so tests
and Playwright E2E can drive scenarios.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from ..state import (
    Art,
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
from .base import DenonAdapter, HeosAdapter, SonosAdapter

FAKE_DENON_HOST = "fake"
HEOS_PLAYER = "heos-1"
KITCHEN = "sonos-RINCON_KITCHEN"
PATIO = "sonos-RINCON_PATIO"


def fake_zone_id(key: str) -> str:
    return f"denon-{FAKE_DENON_HOST}:{key}"


@dataclass(frozen=True)
class FakeTrack:
    title: str
    artist: str
    album: str
    duration_ms: int
    source: str = "tidal"
    art_url: str = "https://example.invalid/art/1.jpg"


DEFAULT_TRACK = FakeTrack("Signal", "Analog Heart", "Warm Glow", 213_000)


class _Ticker:
    """Shared 1 Hz position ticker for fake sides in ``play`` state."""

    def __init__(self, store: StateStore, interval_s: float) -> None:
        self.store = store
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._playing: dict[str, int] = {}

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="fake-ticker")

    async def stop(self) -> None:
        await stop_task(self._task)
        self._task = None

    def set_playing(self, side_id: str, playing: bool) -> None:
        if playing:
            self._playing.setdefault(side_id, 0)
        else:
            self._playing.pop(side_id, None)

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
    """Scripting surface shared by the HEOS and Sonos fakes."""

    # Attributes below are supplied by BaseAdapter / the concrete fake; annotated for typing.
    store: StateStore
    vendor: str
    _ticker: _Ticker
    _connected: bool
    _emit: Callable[..., None]
    _set_status: Callable[..., None]

    def _side_for(self, player_id: str) -> str:
        return side_id_for(self.store.state.players[player_id])

    def set_volume(self, player_id: str, volume: int, muted: bool | None = None) -> None:
        fields: dict[str, object] = {"volume": volume}
        if muted is not None:
            fields["muted"] = muted
        self.store.update_player(player_id, **fields)
        self._emit("volume", player_id=player_id)

    def set_play_state(self, player_id: str, state: PlayState) -> None:
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
                art=Art(url=track.art_url),
                source=track.source,
                seekable=track.source != "pandora",
                duration_ms=track.duration_ms,
            ),
        )
        self.store.set_position(side, Position(position_ms=0, reported_at=now()))
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


class FakeHeos(FakeVendorMixin, HeosAdapter):
    vendor = "heos"

    def __init__(self, store: StateStore, ticker: _Ticker) -> None:
        super().__init__(store)
        self._ticker = ticker
        self._connected = False

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
                capabilities=Capabilities(supports_power=True),
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


class FakeSonos(FakeVendorMixin, SonosAdapter):
    vendor = "sonos"

    def __init__(self, store: StateStore, ticker: _Ticker) -> None:
        super().__init__(store)
        self._ticker = ticker
        self._connected = False

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

    @staticmethod
    def group_id(coordinator: str) -> str:
        return f"sonos-g{coordinator}"

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

    def __init__(self, store: StateStore, tick_interval_s: float = 1.0) -> None:
        self.ticker = _Ticker(store, tick_interval_s)
        self.heos = FakeHeos(store, self.ticker)
        self.sonos = FakeSonos(store, self.ticker)
        self.denon = FakeDenon(store)

    @property
    def adapters(self) -> list[HeosAdapter | SonosAdapter | DenonAdapter]:
        return [self.heos, self.sonos, self.denon]

    async def start(self) -> None:
        for a in self.adapters:
            await a.connect()
        self.ticker.start()

    async def stop(self) -> None:
        await self.ticker.stop()
        for a in self.adapters:
            await a.disconnect()
