from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from illyhub_hub.adapters import heos as heos_mod
from illyhub_hub.adapters.heos import PyHeosAdapter, group_id, player_id
from illyhub_hub.art import placeholder_ref
from illyhub_hub.config import Settings
from illyhub_hub.state import StateStore


@dataclass
class Media:
    supported_controls: list[str] | None = None
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

    calls: list[tuple[str, Any]] = field(default_factory=list)

    def add_on_player_event(self, cb):
        self.callbacks.append(cb)
        return lambda: self.callbacks.remove(cb)

    # pyheos.HeosPlayer command surface (recorded, not executed)
    async def play(self) -> None:
        self.calls.append(("play", None))

    async def pause(self) -> None:
        self.calls.append(("pause", None))

    async def stop(self) -> None:
        self.calls.append(("stop", None))

    async def play_next(self) -> None:
        self.calls.append(("play_next", None))

    async def play_previous(self) -> None:
        self.calls.append(("play_previous", None))

    async def set_volume(self, level: int) -> None:
        self.calls.append(("set_volume", level))

    async def set_mute(self, state: bool) -> None:
        self.calls.append(("set_mute", state))

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
        self.group_calls: list[list[int]] = []

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

    async def set_group(self, player_ids) -> None:
        self.group_calls.append(list(player_ids))

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
    assert np.title == "Signal" and np.seekable is True
    assert (
        np.art == placeholder_ref()
    )  # no ArtCache wired: hub-relative placeholder, never the device URL
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
    # Progress events are edge observations: estimate sits just past the reported second.
    assert 5000 <= store.state.positions["heos:heos-1"].position_ms <= 5100
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

    # An unrecognised report maps to None so callers hold the state they already have,
    # rather than blanking a known state during the ~1s transient at stream start.
    assert heos_mod._play_state(E()) == "pause" and heos_mod._play_state("weird") is None


async def test_commands_map_to_pyheos_player_methods_and_seek_is_unsupported(
    store: StateStore, settings: Settings
) -> None:
    from illyhub_hub.adapters.base import UnsupportedCommandError

    gen = Generations()
    a = make(store, settings, gen)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    raw = gen.players[1]
    await a.play("heos-1")
    await a.pause("heos-1")
    await a.stop("heos-1")
    await a.next("heos-1")
    await a.previous("heos-1")
    await a.set_volume("heos-1", 33)
    await a.set_mute("heos-1", True)
    assert [c[0] for c in raw.calls] == [
        "play",
        "pause",
        "stop",
        "play_next",
        "play_previous",
        "set_volume",
        "set_mute",
    ]
    assert raw.calls[-2] == ("set_volume", 33) and raw.calls[-1] == ("set_mute", True)
    with pytest.raises(UnsupportedCommandError):
        await a.seek("heos-1", 1000)
    assert store.state.players["heos-1"].capabilities.supports_seek is False
    with pytest.raises(ConnectionError):
        await a.play("heos-9")
    await a.set_group(["heos-1", "heos-2"])
    await a.set_group(["heos-2"])
    assert gen.current.group_calls == [[1, 2], [2]]
    with pytest.raises(ValueError):
        heos_mod.raw_pid("heos-g7")
    await a.disconnect()
    with pytest.raises(ConnectionError):
        await a.set_group(["heos-1"])
    with pytest.raises(ConnectionError):
        await a.play("heos-1")


async def test_progress_events_drive_the_position_tracker(
    store: StateStore, settings: Settings
) -> None:
    class Clock:
        t = 500.0

        def __call__(self) -> float:
            return self.t

    clock = Clock()
    gen = Generations()
    a = PyHeosAdapter(store, settings, "192.168.1.20", factory=gen.factory, clock=clock)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    raw = gen.players[1]
    raw.state = "play"
    raw.fire(heos_mod.EVENT_PLAYER_STATE_CHANGED)
    side = "heos:heos-1"
    # One edge event alone is low confidence; consistent ~1 s spaced events converge.
    raw.now_playing_media = Media(current_position=1000)
    raw.fire(heos_mod.EVENT_NOW_PLAYING_PROGRESS)
    assert store.state.positions[side].confidence == 0.5
    for pos in (2000, 3000):
        clock.t += 1.0 + 0.03  # event arrives ~30 ms after the device tick
        raw.now_playing_media = Media(current_position=pos)
        raw.fire(heos_mod.EVENT_NOW_PLAYING_PROGRESS)
    p = store.state.positions[side]
    assert p.confidence == 0.9 and abs(p.position_ms - 3030) < 120
    # A seek from the vendor app contradicts the anchor: back to a single observation.
    clock.t += 1.0
    raw.now_playing_media = Media(current_position=60_000)
    raw.fire(heos_mod.EVENT_NOW_PLAYING_PROGRESS)
    assert store.state.positions[side].confidence == 0.5
    assert a._trackers[side].observations == 1
    raw.state = "pause"
    raw.fire(heos_mod.EVENT_PLAYER_STATE_CHANGED)
    raw.now_playing_media = Media(current_position=60_000)
    raw.fire(heos_mod.EVENT_NOW_PLAYING_PROGRESS)
    assert store.state.positions[side].position_ms == 60_000  # paused: raw value
    await a.disconnect()


async def test_dissolve_uses_the_leader_and_controls_set_capabilities(
    store: StateStore, settings: Settings
) -> None:
    gen = Generations()
    a = make(store, settings, gen)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    await a.dissolve("heos-1", ["heos-1", "heos-2"])
    assert gen.current.group_calls == [[1]]  # leader pid only, never the followers
    raw = gen.players[1]
    raw.now_playing_media = Media(type="station", supported_controls=["play", "play_next"])
    raw.fire(heos_mod.EVENT_NOW_PLAYING_CHANGED)
    np = store.state.now_playing["heos:heos-1"]
    assert np.supports_next is True and np.supports_prev is False and np.seekable is False
    side = store.state.sides["heos:heos-1"]
    assert side.capabilities.supports_prev is False and side.capabilities.supports_next is True
    raw.now_playing_media = Media(type="station")  # no supported_controls attribute -> by type
    raw.fire(heos_mod.EVENT_NOW_PLAYING_CHANGED)
    assert store.state.now_playing["heos:heos-1"].supports_prev is False
    assert heos_mod._enum_value(None) == ""
    await a.disconnect()


async def test_missing_player_event_hook_is_logged(
    store: StateStore, settings: Settings, caplog
) -> None:
    import logging

    caplog.set_level(logging.WARNING, logger="adapter.heos")
    gen = Generations()
    gen.players[1].add_on_player_event = None  # type: ignore[assignment]  # hook missing
    a = make(store, settings, gen)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    assert "no add_on_player_event" in caplog.text
    await a.disconnect()


async def test_now_playing_art_registers_device_url_with_cache(
    store: StateStore, settings: Settings, tmp_path
) -> None:
    """With a cache wired, the HEOS image_url becomes the registered source and the state carries
    a hub-relative ArtRef with the cache key."""
    from illyhub_hub.art import ArtCache, ArtHelper

    cache = ArtCache(tmp_path / "art")
    gen = Generations()
    a = PyHeosAdapter(store, settings, "192.168.1.20", factory=gen.factory, art=ArtHelper(cache))
    await a.connect()
    await wait_for(lambda: bool(store.state.now_playing))
    np = next(iter(store.state.now_playing.values()))
    assert np.art.url == f"/api/art/{np.art.cache_key}" and np.art.cache_key
    assert cache._read_meta(np.art.cache_key).source_url == "http://art/1.jpg"
    await a.disconnect()


# -- Phase 3: play by canonical id ---------------------------------------------------------


class Source:
    def __init__(self, available: bool) -> None:
        self.available = available


async def test_play_content_uses_add_to_queue_and_music_sources(
    store: StateStore, settings: Settings
) -> None:
    from illyhub_hub.adapters.base import ContentUnavailableError, PlayableTrack
    from illyhub_hub.content import ContentRef

    gen = Generations()
    queue_calls: list[tuple[int, str, str | None, int]] = []
    play_queue_calls: list[int] = []
    queue_len = {"n": 0}
    get_queue_calls: list[tuple[int, int]] = []

    async def add_to_queue(source_id, container_id, media_id=None, add_criteria=None):
        queue_calls.append((source_id, container_id, media_id, int(add_criteria)))
        queue_len["n"] = 0  # the container loads asynchronously on a real receiver

    async def get_queue(start, end):
        get_queue_calls.append((start, end))
        queue_len["n"] += 1  # one more item lands per poll
        return list(range(queue_len["n"]))[start : end + 1]  # 0-based inclusive, like pyheos

    async def play_queue(qid: int) -> None:
        play_queue_calls.append(qid)

    for p in gen.players.values():
        p.add_to_queue = add_to_queue  # type: ignore[attr-defined]
        p.play_queue = play_queue  # type: ignore[attr-defined]
        p.get_queue = get_queue  # type: ignore[attr-defined]
    sources = {10: Source(True), 1: Source(False)}
    FakeHeosClient.get_music_sources = lambda self, *, refresh=False: _ret(sources)  # type: ignore[attr-defined]
    a = make(store, settings, gen)
    try:
        await a.connect()
        await wait_for(lambda: a.status().state == "connected")
        assert a.service_linked("tidal") is True and a.service_linked("pandora") is False
        assert a.service_linked("ytmusic") is False
        tracks = [
            PlayableTrack(service="tidal", track_id="10101", title="Signal", album_id="101"),
            PlayableTrack(service="tidal", track_id="10102", title="Carrier", album_id="101"),
            PlayableTrack(service="tidal", track_id="10103", title="Sideband", album_id="101"),
        ]
        album = ContentRef(service="tidal", kind="album", id="101")
        await a.play_content("heos-1", album, tracks, start_index=0)
        assert queue_calls == [(10, "101", None, 4)] and play_queue_calls == []
        # start_index waits for the queue to contain enough items before play_queue.
        await a.play_content("heos-1", album, tracks, start_index=2)
        assert play_queue_calls == [3]  # HEOS queue ids are 1-based; items carry none here
        assert len(get_queue_calls) == 3 and get_queue_calls[-1] == (0, 2)
        await a.play_content(
            "heos-1", ContentRef(service="tidal", kind="track", id="10102"), tracks, 1
        )
        assert queue_calls[-1] == (10, "101", "10102", 4)
        with pytest.raises(IndexError):
            await a.play_content("heos-1", album, tracks, 9)
        sources[10].available = False
        await a._load_sources(gen.current)
        with pytest.raises(ContentUnavailableError):
            await a.play_content("heos-1", album, tracks, 0)
        await a.disconnect()
        assert a._sources == {}  # teardown forgets the sources
    finally:
        del FakeHeosClient.get_music_sources  # type: ignore[attr-defined]


async def test_play_content_start_index_never_silently_dropped(
    store: StateStore, settings: Settings
) -> None:
    from illyhub_hub.adapters.base import PlayableTrack
    from illyhub_hub.content import ContentRef

    gen = Generations()

    async def add_to_queue(*a, **k):
        return None

    for p in gen.players.values():
        p.add_to_queue = add_to_queue  # type: ignore[attr-defined]
    sources = {10: Source(True)}
    FakeHeosClient.get_music_sources = lambda self, *, refresh=False: _ret(sources)  # type: ignore[attr-defined]
    a = make(store, settings, gen)
    try:
        await a.connect()
        await wait_for(lambda: a.status().state == "connected")
        tracks = [PlayableTrack(service="tidal", track_id=str(i), title=f"T{i}") for i in range(3)]
        album = ContentRef(service="tidal", kind="album", id="101")
        with pytest.raises(RuntimeError, match="start_index dropped"):
            await a.play_content("heos-1", album, tracks, start_index=1)  # no play_queue

        async def play_queue(qid):
            return None

        async def get_queue(start, end):
            return []  # never fills

        for p in gen.players.values():
            p.play_queue = play_queue  # type: ignore[attr-defined]
            p.get_queue = get_queue  # type: ignore[attr-defined]
        a.QUEUE_WAIT_S = 0.3
        with pytest.raises(TimeoutError):
            await a.play_content("heos-1", album, tracks, start_index=1)
        await a.disconnect()
    finally:
        del FakeHeosClient.get_music_sources  # type: ignore[attr-defined]


async def _ret(value):
    return value


async def test_music_sources_failure_means_nothing_linked(
    store: StateStore, settings: Settings
) -> None:
    gen = Generations()

    async def boom(self, *, refresh=False):
        raise RuntimeError("no sources")

    FakeHeosClient.get_music_sources = boom  # type: ignore[attr-defined]
    try:
        a = make(store, settings, gen)
        await a.connect()
        await wait_for(lambda: a.status().state == "connected")
        assert a.service_linked("tidal") is False
        await a.disconnect()
    finally:
        del FakeHeosClient.get_music_sources  # type: ignore[attr-defined]


async def test_prime_content_loads_without_playing_and_start_primed_plays_queue(
    store: StateStore, settings: Settings
) -> None:
    """Sync Play priming on HEOS: clear_queue → add_to_queue(ADD_TO_END) → wait for the queue →
    describe the queued item; start_primed → play_queue (1-based)."""
    from illyhub_hub.adapters.base import ContentUnavailableError, PlayableTrack
    from illyhub_hub.content import ContentRef

    gen = Generations()
    calls: list[tuple[str, tuple]] = []
    queue_len = {"n": 0}

    class Item:
        def __init__(self, media_id: str, song: str, queue_id: int) -> None:
            self.media_id, self.song, self.queue_id = media_id, song, queue_id

    async def clear_queue() -> None:
        calls.append(("clear_queue", ()))
        queue_len["n"] = 0

    async def add_to_queue(source_id, container_id, media_id=None, add_criteria=None):
        calls.append(("add_to_queue", (source_id, container_id, media_id, int(add_criteria))))

    async def get_queue(start, end):
        queue_len["n"] += 2
        # 0-based inclusive range; queue ids on the receiver are 1-based and offset by 10 here
        # so the test proves play_queue uses the item's queue_id, not index + 1.
        items = [Item(f"1010{i + 1}", f"S{i + 1}", 10 + i + 1) for i in range(queue_len["n"])]
        return items[start : end + 1]

    async def play_queue(qid: int) -> None:
        calls.append(("play_queue", (qid,)))

    for p in gen.players.values():
        p.clear_queue = clear_queue  # type: ignore[attr-defined]
        p.add_to_queue = add_to_queue  # type: ignore[attr-defined]
        p.get_queue = get_queue  # type: ignore[attr-defined]
        p.play_queue = play_queue  # type: ignore[attr-defined]
    FakeHeosClient.get_music_sources = lambda self, *, refresh=False: _ret({10: Source(True)})  # type: ignore[attr-defined]
    a = make(store, settings, gen)
    try:
        await a.connect()
        await wait_for(lambda: a.status().state == "connected")
        tracks = [
            PlayableTrack(
                service="tidal", track_id=f"1010{i + 1}", title=f"S{i + 1}", album_id="101"
            )
            for i in range(4)
        ]
        album = ContentRef(service="tidal", kind="album", id="101")
        primed = await a.prime_content("heos-1", album, tracks, start_index=2)
        assert primed.track_id == "10103" and primed.title == "S3"
        assert calls[:2] == [("clear_queue", ()), ("add_to_queue", (10, "101", None, 3))]
        assert not any(c[0] == "play_queue" for c in calls)  # nothing started
        await a.start_primed("heos-1", 2)
        assert calls[-1] == ("play_queue", (13,))  # the item's own queue id, not index + 1
        # a single track primes with the album container + media id
        primed = await a.prime_content(
            "heos-1", ContentRef(service="tidal", kind="track", id="10102"), tracks, 1
        )
        assert primed.track_id == "10101" or primed.title  # whatever sits first in the queue
        assert calls[-1] == ("add_to_queue", (10, "101", "10102", 3))
        with pytest.raises(IndexError):
            await a.prime_content("heos-1", album, tracks, 9)
        # a client without clear_queue / play_queue cannot prime or start
        for p in gen.players.values():
            del p.clear_queue  # type: ignore[attr-defined]
            del p.play_queue  # type: ignore[attr-defined]
        with pytest.raises(RuntimeError):
            await a.prime_content("heos-1", album, tracks, 0)
        with pytest.raises(RuntimeError):
            await a.start_primed("heos-1", 0)
        a._sources = {}
        with pytest.raises(ContentUnavailableError):
            await a.prime_content("heos-1", album, tracks, 0)
        await a.disconnect()
    finally:
        del FakeHeosClient.get_music_sources  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------
# Phase 5: Pandora stations via browse/browse + browse/play_stream
# --------------------------------------------------------------------------------------


def Item(  # noqa: N802 - factory named like the class it builds
    name: str,
    *,
    type: str = "station",  # noqa: A002 - mirrors the CLI field
    playable: bool = True,
    browsable: bool = False,
    container_id: str | None = None,
    media_id: str | None = None,
    image_url: str = "",
):
    """A real ``pyheos.MediaItem`` built from the CLI's raw payload fields, so the adapter is
    exercised against the library's own parsing (``playable``/``container`` are yes/no strings,
    ids are ``cid``/``mid``)."""
    from pyheos.media import MediaItem

    data: dict[str, Any] = {
        "name": name,
        "type": type,
        "image_url": image_url,
        "playable": "yes" if playable else "no",
    }
    if browsable:
        data["container"] = "yes"
    if container_id is not None:
        data["cid"] = container_id
    if media_id is not None:
        data["mid"] = media_id
    return MediaItem.from_data(data, source_id=1)


class Browse:
    def __init__(self, items: list[Any]) -> None:
        self.items = items


async def test_list_and_play_stations_use_browse_and_play_stream(
    store: StateStore, settings: Settings
) -> None:
    from illyhub_hub.adapters.base import ContentUnavailableError
    from illyhub_hub.content import ContentRef

    gen = Generations()
    browse_calls: list[tuple[int, str | None]] = []
    play_calls: list[tuple[int, int, str | None, str]] = []
    tree = {
        None: [
            Item("My Stations", type="container", browsable=True, container_id="my"),
            Item("Thumbprint Radio", media_id="tp", image_url="http://art/tp.jpg"),
            Item("Search", type="container", browsable=True, container_id="search"),
        ],
        "my": [
            Item("Chill Radio", container_id="my", media_id="c1", image_url="http://art/c.jpg"),
            Item("Chill Radio", container_id="my", media_id="c1"),  # duplicate (cid, mid)
            Item("Shuffle", type="container", browsable=True, container_id="deeper"),
            Item("Ad", playable=False, media_id="ad"),
        ],
        "search": [],
        "broken": None,  # this folder's browse raises; the rest must still come back
    }
    tree[None].insert(
        1, Item("Broken folder", type="container", browsable=True, container_id="broken")
    )

    async def browse(source_id, container_id=None, range_start=None, range_end=None):
        browse_calls.append((source_id, container_id))
        if tree.get(container_id, []) is None:
            raise TimeoutError("receiver busy")
        return Browse(tree[container_id])

    async def play_station(player_id, source_id, container_id, media_id):
        play_calls.append((player_id, source_id, container_id, media_id))

    sources = {10: Source(True), 1: Source(True)}
    FakeHeosClient.get_music_sources = lambda self, *, refresh=False: _ret(sources)  # type: ignore[attr-defined]
    FakeHeosClient.browse = lambda self, *a, **k: browse(*a, **k)  # type: ignore[attr-defined]
    FakeHeosClient.play_station = lambda self, *a: play_station(*a)  # type: ignore[attr-defined]
    a = make(store, settings, gen)
    try:
        await a.connect()
        await wait_for(lambda: a.status().state == "connected")
        stations = await a.list_stations("pandora")
        assert [s.name for s in stations] == ["Thumbprint Radio", "Chill Radio"]  # deduped
        assert stations[1].ids == {"sid": "1", "cid": "my", "mid": "c1"}
        assert stations[1].art_url == "http://art/c.jpg" and stations[0].ids["cid"] == ""
        # root + one level of containers (a raising folder is skipped), never deeper
        assert browse_calls == [(1, None), (1, "my"), (1, "broken"), (1, "search")]

        ref = ContentRef(service="pandora", kind="station", id="chill-radio")
        await a.play_station("heos-1", ref, stations[1])
        assert play_calls == [(1, 1, "my", "c1")]
        np = store.state.now_playing["heos:heos-1"]
        assert np.title == "Chill Radio" and np.source == "pandora" and np.content_ref == ref
        assert np.seekable is False and np.supports_prev is False and np.supports_next is True
        assert store.state.positions["heos:heos-1"].confidence == 0.5
        assert store.state.players["heos-1"].play_state == "play"  # optimistic, like Sonos

        # The receiver's now-playing event (a Pandora station) keeps the station identity and
        # reports the service by NAME, not by source id.
        raw = gen.players[1]
        raw.now_playing_media = Media(
            song="Some Track", type="station", source_id=1, media_id="x", album="Chill Radio"
        )
        raw.fire(heos_mod.EVENT_NOW_PLAYING_CHANGED)
        np = store.state.now_playing["heos:heos-1"]
        assert np.title == "Some Track" and np.content_ref == ref and np.supports_next is True
        assert np.source == "pandora"
        # Another station chosen in the HEOS app (different station name) drops the stale ref.
        raw.now_playing_media = Media(
            song="Other", type="station", source_id=1, media_id="y", album="Jazz Nights"
        )
        raw.fire(heos_mod.EVENT_NOW_PLAYING_CHANGED)
        assert store.state.now_playing["heos:heos-1"].content_ref is None
        assert store.state.now_playing["heos:heos-1"].source == "pandora"
        # Re-play, then a Tidal track from the vendor app drops it too (source is a name).
        await a.play_station("heos-1", ref, stations[1])
        raw.now_playing_media = Media(song="Signal", type="song", source_id=10, media_id="555")
        raw.fire(heos_mod.EVENT_NOW_PLAYING_CHANGED)
        np = store.state.now_playing["heos:heos-1"]
        assert np.content_ref is not None and np.content_ref.service == "tidal"
        assert np.source == "tidal" and "heos:heos-1" not in a._station_playing
        # An unknown source id still yields a string, never a swallowed None.
        raw.now_playing_media = Media(song="Line in", type="song", source_id=1024, media_id="")
        raw.fire(heos_mod.EVENT_NOW_PLAYING_CHANGED)
        assert store.state.now_playing["heos:heos-1"].source == "1024"

        # A vendor failure during play_station restores the previous now-playing.
        before = store.state.now_playing["heos:heos-1"]

        async def boom(*_a):
            raise TimeoutError("receiver busy")

        FakeHeosClient.play_station = lambda self, *a: boom(*a)  # type: ignore[attr-defined]
        with pytest.raises(TimeoutError):
            await a.play_station("heos-1", ref, stations[1])
        assert store.state.now_playing["heos:heos-1"] == before
        assert "heos:heos-1" not in a._station_playing
        FakeHeosClient.play_station = lambda self, *a: play_station(*a)  # type: ignore[attr-defined]

        # Missing ids / unlinked service refuse cleanly.
        with pytest.raises(ContentUnavailableError):
            await a.play_station("heos-1", ref, stations[1].model_copy(update={"ids": {}}))
        sources[1].available = False
        await a._load_sources(gen.current)
        with pytest.raises(ContentUnavailableError):
            await a.list_stations("pandora")
        with pytest.raises(ContentUnavailableError):
            await a.play_station("heos-1", ref, stations[1])
        await a.disconnect()
    finally:
        del FakeHeosClient.get_music_sources  # type: ignore[attr-defined]
        del FakeHeosClient.browse  # type: ignore[attr-defined]
        del FakeHeosClient.play_station  # type: ignore[attr-defined]


async def test_station_calls_without_browse_support_refuse(
    store: StateStore, settings: Settings
) -> None:
    from illyhub_hub.adapters.base import ContentUnavailableError, VendorStation
    from illyhub_hub.content import ContentRef

    gen = Generations()
    sources = {1: Source(True)}
    FakeHeosClient.get_music_sources = lambda self, *, refresh=False: _ret(sources)  # type: ignore[attr-defined]
    a = make(store, settings, gen)
    try:
        await a.connect()
        await wait_for(lambda: a.status().state == "connected")
        with pytest.raises(ContentUnavailableError, match="not ready"):
            await a.list_stations("pandora")
        station = VendorStation(vendor="heos", name="X", ids={"sid": "1", "mid": "m"})
        with pytest.raises(ContentUnavailableError, match="cannot play stations"):
            await a.play_station(
                "heos-1", ContentRef(service="pandora", kind="station", id="x"), station
            )
        await a.disconnect()
    finally:
        del FakeHeosClient.get_music_sources  # type: ignore[attr-defined]


async def test_avr_owned_volume_is_not_overwritten_by_heos_reports(
    store: StateStore, settings: Settings
) -> None:
    """A Denon zone owning this player makes HEOS's volume and mute reports untrustworthy.

    On an AVR-X3400H, HEOS reports volume 0 no matter what the receiver's MV is, so following
    those reports would blank the mirrored level and skew linked volume (VOL-2). Everything
    else in the same event still applies.
    """
    from illyhub_hub.state import Zone

    gen = Generations()
    a = make(store, settings, gen)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")

    # The Denon adapter claims the player and mirrors the amplifier's real level onto it.
    store.set_zone(
        Zone(
            id="denon-192.168.1.20:main",
            key="main",
            name="Main zone",
            host="192.168.1.20",
            player_ids=["heos-1"],
            volume=55,
        )
    )
    store.update_player("heos-1", volume=55, muted=False)

    raw = gen.players[1]
    raw.volume, raw.is_muted = 0, True  # what HEOS actually reports for an AVR
    raw.state = "play"
    raw.fire(heos_mod.EVENT_PLAYER_VOLUME_CHANGED)
    p = store.state.players["heos-1"]
    assert p.volume == 55 and p.muted is False  # zone stays the authority

    # A zone on a fixed pre-out does not own the level, so HEOS is believed again.
    zone = store.state.zones["denon-192.168.1.20:main"]
    store.set_zone(zone.model_copy(update={"supports_volume": False}))
    raw.volume = 12
    raw.fire(heos_mod.EVENT_PLAYER_VOLUME_CHANGED)
    assert store.state.players["heos-1"].volume == 12
    await a.disconnect()


async def test_transient_state_report_holds_the_known_play_state(
    store: StateStore, settings: Settings
) -> None:
    """HEOS answers state=unknown for ~1s while a stream starts; that must not blank state.

    Measured on an AVR-X3400H: `t+01s state=unknown`, `t+02s state=play`. Writing "unknown"
    through would overwrite the client's optimistic play and make the button flick back.
    """
    gen = Generations()
    a = make(store, settings, gen)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    raw = gen.players[1]

    raw.state = "play"
    raw.fire(heos_mod.EVENT_PLAYER_STATE_CHANGED)
    assert store.state.players["heos-1"].play_state == "play"

    raw.state = "unknown"  # the transient
    raw.fire(heos_mod.EVENT_PLAYER_STATE_CHANGED)
    assert store.state.players["heos-1"].play_state == "play"  # held, not blanked

    raw.state = "pause"  # a real change still lands
    raw.fire(heos_mod.EVENT_PLAYER_STATE_CHANGED)
    assert store.state.players["heos-1"].play_state == "pause"
    await a.disconnect()
