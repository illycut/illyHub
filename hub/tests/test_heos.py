from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from illyhub_hub.adapters import heos as heos_mod
from illyhub_hub.adapters.heos import PyHeosAdapter, group_id, player_id
from illyhub_hub.config import Settings
from illyhub_hub.state import StateStore


@dataclass
class Media:
    song: str = "Signal"
    artist: str = "Analog Heart"
    album: str = "Warm Glow"
    image_url: str = "http://art/1.jpg"
    source_id: int = 10
    type: str = "song"
    media_id: str = "12345"
    duration: int = 213000
    current_position: int = 0


@dataclass
class RawPlayer:
    name: str
    player_id: int
    ip_address: str = "192.168.1.20"
    model: str = "AVR"
    available: bool = True
    volume: int = 30
    is_muted: bool = False
    state: str = "stop"
    now_playing_media: Media = field(default_factory=Media)
    callbacks: list[Any] = field(default_factory=list)

    def add_on_player_event(self, cb):
        self.callbacks.append(cb)
        return lambda: self.callbacks.remove(cb)

    def fire(self, event: str) -> None:
        for cb in list(self.callbacks):
            cb(event)


@dataclass
class RawGroup:
    """Mirrors pyheos.HeosGroup: member_player_ids holds followers only, never the leader."""

    group_id: int
    lead_player_id: int
    member_player_ids: list[int]
    name: str = "Everywhere"


class FakeHeosClient:
    def __init__(self, gen: Generations, fail: bool = False) -> None:
        self.gen = gen
        self.players = gen.players
        self.groups = gen.groups
        self.fail = fail
        self.disconnected_cbs: list[Any] = []
        self.controller_cbs: list[Any] = []
        self.disconnect_calls = 0

    async def connect(self) -> None:
        if self.fail:
            raise ConnectionError("refused")

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def get_players(self, *, refresh: bool = False):
        return self.players

    async def get_groups(self, *, refresh: bool = False):
        return self.groups

    def add_on_connected(self, cb):
        return lambda: None

    def add_on_disconnected(self, cb):
        self.disconnected_cbs.append(cb)
        return lambda: None

    def add_on_controller_event(self, cb):
        self.controller_cbs.append(cb)
        return lambda: None

    def drop(self) -> None:
        for cb in self.disconnected_cbs:
            cb()

    def controller(self, event: str) -> None:
        for cb in self.controller_cbs:
            cb(event, None)


class Generations:
    """Factory that mints a fresh client per connect, like ``pyheos.Heos`` would be re-created."""

    def __init__(self, fail_first: int = 0) -> None:
        self.players = {1: RawPlayer("Living Room Amp", 1), 2: RawPlayer("Den", 2, volume=12)}
        self.groups: dict[int, RawGroup] = {}
        self.instances: list[FakeHeosClient] = []
        self.fail_first = fail_first

    def factory(self, _host: str, _settings: Settings) -> FakeHeosClient:
        fail = len(self.instances) < self.fail_first
        client = FakeHeosClient(self, fail=fail)
        self.instances.append(client)
        return client

    @property
    def current(self) -> FakeHeosClient:
        return self.instances[-1]


def make(store: StateStore, settings: Settings, gen: Generations) -> PyHeosAdapter:
    return PyHeosAdapter(store, settings, "192.168.1.20", factory=gen.factory)


async def wait_for(pred, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.005)


async def test_connect_loads_players_now_playing_and_single_socket_invariant(
    store: StateStore, settings: Settings
) -> None:
    gen = Generations()
    a = make(store, settings, gen)
    await a.connect()
    await a.connect()  # must not mint a second client
    await wait_for(lambda: a.status().state == "connected")
    assert len(gen.instances) == 1
    assert set(store.state.players) == {"heos-1", "heos-2"}
    lr = store.state.players["heos-1"]
    assert lr.name == "Living Room Amp" and lr.volume == 30
    assert lr.capabilities.supports_power is True
    np = store.state.now_playing["heos:heos-1"]
    assert np.title == "Signal" and np.art.url == "http://art/1.jpg" and np.seekable is True
    assert np.duration_ms == 213000 and np.track_id == "12345"
    await a.disconnect()
    assert a.status().state == "disconnected" and gen.current.disconnect_calls == 1


async def test_group_topology_includes_leader_and_no_phantom_solo_side(
    store: StateStore, settings: Settings
) -> None:
    gen = Generations()
    gen.groups = {7: RawGroup(7, 1, [2])}  # leader 1, follower 2 (pyheos shape)
    a = make(store, settings, gen)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    topo = store.state.groups[group_id(7)]
    assert topo.coordinator_player_id == "heos-1" and topo.member_ids == ["heos-1", "heos-2"]
    assert store.state.players["heos-1"].group_id == group_id(7)
    assert store.state.players["heos-2"].group_id == group_id(7)
    assert set(store.state.sides) == {"heos:heos-g7"}  # no phantom heos:heos-1 side
    assert store.state.sides["heos:heos-g7"].name == "Everywhere"
    # now-playing for a grouped player lands on the group side
    assert (
        "heos:heos-g7" in store.state.now_playing and "heos:heos-1" not in store.state.now_playing
    )
    await a.disconnect()


async def test_player_events_update_state_and_bad_event_is_contained(
    store: StateStore, settings: Settings
) -> None:
    gen = Generations()
    a = make(store, settings, gen)
    kinds: list[str] = []
    a.on_event(lambda e: kinds.append(e.kind))
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    raw = gen.players[1]
    raw.state = "play"
    raw.fire(heos_mod.EVENT_PLAYER_STATE_CHANGED)
    raw.volume, raw.is_muted = 45, True
    raw.fire(heos_mod.EVENT_PLAYER_VOLUME_CHANGED)
    raw.now_playing_media = Media(song="Next", type="station", current_position=5000)
    raw.fire(heos_mod.EVENT_NOW_PLAYING_CHANGED)
    raw.fire(heos_mod.EVENT_NOW_PLAYING_PROGRESS)
    p = store.state.players["heos-1"]
    assert p.play_state == "play" and p.volume == 45 and p.muted is True
    assert store.state.now_playing["heos:heos-1"].title == "Next"
    assert store.state.now_playing["heos:heos-1"].seekable is False
    assert store.state.sides["heos:heos-1"].capabilities.supports_seek is False
    assert store.state.positions["heos:heos-1"].position_ms == 5000
    assert {"connected", "play_state", "volume", "now_playing"} <= set(kinds)
    raw.volume = "not-a-number"
    raw.fire(heos_mod.EVENT_PLAYER_VOLUME_CHANGED)  # logged, not raised
    assert store.state.players["heos-1"].volume == 45
    await a.disconnect()


async def test_groups_changed_refreshes_topology_and_removes_stale(
    store: StateStore, settings: Settings
) -> None:
    gen = Generations()
    a = make(store, settings, gen)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    gen.groups[7] = RawGroup(7, 1, [3])
    del gen.players[2]
    gen.players[3] = RawPlayer("Patio Amp", 3)
    gen.current.controller(heos_mod.EVENT_GROUPS_CHANGED)
    await wait_for(lambda: group_id(7) in store.state.groups)
    await wait_for(lambda: "heos-2" not in store.state.players)
    assert store.state.sides["heos:heos-g7"].member_ids == ["heos-1", "heos-3"]
    gen.current.controller("event/other")  # ignored
    await a.disconnect()


async def test_topology_refresh_failure_is_logged(store: StateStore, settings: Settings) -> None:
    gen = Generations()
    a = make(store, settings, gen)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")

    async def boom(*, refresh: bool = False):
        raise RuntimeError("cli error")

    gen.current.get_groups = boom  # type: ignore[method-assign]
    gen.current.controller(heos_mod.EVENT_PLAYERS_CHANGED)
    await asyncio.sleep(0.02)
    assert a.status().state == "connected"
    await a.disconnect()


async def test_reconnect_mints_new_client_per_generation(
    store: StateStore, settings: Settings
) -> None:
    gen = Generations(fail_first=1)
    a = make(store, settings, gen)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected", timeout=2)
    assert len(gen.instances) == 2  # first generation failed, second connected
    assert a._backoff.attempt == 1  # short-lived connection: backoff NOT reset
    gen.current.drop()
    await wait_for(lambda: len(gen.instances) == 3, timeout=2)
    await wait_for(lambda: a.status().state == "connected", timeout=2)
    assert store.state.players["heos-1"].online is True
    await a.disconnect()
    assert sum(c.disconnect_calls for c in gen.instances) == len(gen.instances)


async def test_backoff_resets_after_long_hold(store: StateStore, settings: Settings) -> None:
    gen = Generations()
    a = make(store, settings, gen)
    a._backoff.attempt = 5
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    a._connected_at -= settings.backoff_reset_after_s + 1  # pretend it stayed up long enough
    gen.current.drop()
    await wait_for(lambda: len(gen.instances) == 2, timeout=2)
    assert a._backoff.attempt <= 1
    await a.disconnect()


def test_helpers() -> None:
    assert player_id(5) == "heos-5" and group_id(3) == "heos-g3"
    assert heos_mod._play_state("play") == "play"

    class E:
        value = "pause"

    assert heos_mod._play_state(E()) == "pause" and heos_mod._play_state("weird") == "unknown"
