"""Phase 8 adapters: HEOS and Sonos queue reads / jumps / play mode against test doubles shaped
like pyheos and SoCo, plus the events that keep ``HubState.queues`` and ``play_mode`` fresh."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from illyhub_hub.adapters import heos as heos_mod
from illyhub_hub.adapters import sonos as sonos_mod
from illyhub_hub.adapters.base import UnsupportedCommandError
from illyhub_hub.adapters.sonos import play_mode_from_sonos, play_mode_to_sonos
from illyhub_hub.config import Settings
from illyhub_hub.state import PlayMode, StateStore
from test_heos import Generations, RawPlayer, make
from test_heos import wait_for as heos_wait
from test_sonos import Event, Harness


@dataclass
class QueueItem:
    """pyheos.QueueItem shape."""

    queue_id: int
    song: str
    album: str = "Warm Glow"
    artist: str = "Analog Heart"
    image_url: str = "http://art/q.jpg"
    media_id: str = "0"
    album_id: str = "101"


@dataclass
class QueuePlayer(RawPlayer):
    """RawPlayer plus the pyheos queue/play-mode surface."""

    queue: list[QueueItem] = field(default_factory=list)
    repeat: str = "off"
    shuffle: bool = False
    queue_calls: list[tuple[int | None, int | None]] = field(default_factory=list)

    async def get_queue(self, range_start: int | None = None, range_end: int | None = None):
        self.queue_calls.append((range_start, range_end))
        start = range_start or 0
        end = len(self.queue) - 1 if range_end is None else range_end
        return self.queue[start : end + 1]

    async def play_queue(self, queue_id: int) -> None:
        self.calls.append(("play_queue", queue_id))

    async def set_play_mode(self, repeat: Any, shuffle: bool) -> None:
        self.calls.append(("set_play_mode", (str(getattr(repeat, "value", repeat)), shuffle)))
        self.repeat = str(getattr(repeat, "value", repeat))
        self.shuffle = shuffle


def big_queue(n: int) -> list[QueueItem]:
    return [
        QueueItem(queue_id=i + 1, song=f"Song {i + 1}", media_id=str(10100 + i)) for i in range(n)
    ]


async def test_heos_queue_pages_by_100_flags_truncation_and_maps_items(
    store: StateStore, settings: Settings
) -> None:
    gen = Generations()
    gen.players = {1: QueuePlayer("Living Room Amp", 1, queue=big_queue(250))}
    a = make(store, settings, gen)
    await a.connect()
    await heos_wait(lambda: a.status().state == "connected")
    entries, total = await a.get_queue("heos-1", limit=200)
    # The last page came back full, so there may be more: total unknown. (The CLI itself does
    # report `count`; pyheos drops it -- see HeosAdapter.get_queue.)
    assert total is None and len(entries) == 200
    assert gen.players[1].queue_calls[:2] == [(0, 99), (100, 199)]
    first = entries[0]
    assert first.index == 0 and first.title == "Song 1" and first.track_id == "10100"
    # The playing media's source (Tidal, sid 10) names the queue's service; never assumed.
    assert first.content_ref is not None and first.content_ref.id == "10100"
    assert first.content_ref.service == "tidal"
    entries, total = await a.get_queue("heos-1", limit=5)
    assert len(entries) == 5 and total is None  # 5 requested, 5 returned: still unknown
    gen.players[1].queue = big_queue(7)
    entries, total = await a.get_queue("heos-1", limit=200)
    assert total == 7 and len(entries) == 7  # a short page: the end was seen
    await a.disconnect()


async def test_heos_play_queue_index_uses_receiver_queue_ids(
    store: StateStore, settings: Settings
) -> None:
    gen = Generations()
    p = QueuePlayer(
        "Living Room Amp", 1, queue=[QueueItem(7, "A"), QueueItem(9, "B"), QueueItem(11, "C")]
    )
    gen.players = {1: p}
    a = make(store, settings, gen)
    await a.connect()
    await heos_wait(lambda: a.status().state == "connected")
    await a.play_queue_index("heos-1", 2)  # no prior read: re-reads, then uses the item's id
    assert ("play_queue", 11) in p.calls
    with pytest.raises(IndexError):
        await a.play_queue_index("heos-1", 9)
    await a.disconnect()


async def test_heos_play_mode_get_set_and_events(store: StateStore, settings: Settings) -> None:
    gen = Generations()
    p = QueuePlayer("Living Room Amp", 1, repeat="on_all", shuffle=True)
    gen.players = {1: p}
    a = make(store, settings, gen)
    await a.connect()
    await heos_wait(lambda: a.status().state == "connected")
    mode = await a.get_play_mode("heos-1")
    assert mode.model_dump() == {"shuffle": True, "repeat": "all"}
    await a.set_play_mode("heos-1", PlayMode(shuffle=False, repeat="one"))
    assert ("set_play_mode", ("on_one", False)) in p.calls
    assert store.state.players["heos-1"].play_mode.repeat == "one"
    # the receiver announces a change made from the HEOS app
    p.repeat, p.shuffle = "off", True
    p.fire(heos_mod.EVENT_SHUFFLE_MODE_CHANGED)
    assert store.state.players["heos-1"].play_mode.model_dump() == {
        "shuffle": True,
        "repeat": "off",
    }
    assert store.state.sides["heos:heos-1"].play_mode.shuffle is True
    await a.disconnect()


async def test_heos_queue_event_debounces_into_one_reread(
    store: StateStore, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(heos_mod, "QUEUE_DEBOUNCE_S", 0.02)
    gen = Generations()
    p = QueuePlayer("Living Room Amp", 1, queue=big_queue(3))
    gen.players = {1: p}
    a = make(store, settings, gen)
    await a.connect()
    await heos_wait(lambda: a.status().state == "connected")
    for _ in range(5):
        p.fire(heos_mod.EVENT_QUEUE_CHANGED)
    await heos_wait(lambda: "heos:heos-1" in store.state.queues)
    await asyncio.sleep(0.05)
    assert len(p.queue_calls) == 1  # five events, one read
    q = store.state.queues["heos:heos-1"]
    assert [e.title for e in q.items] == ["Song 1", "Song 2", "Song 3"] and q.total == 3
    await a.disconnect()


async def test_heos_without_queue_support_refuses(store: StateStore, settings: Settings) -> None:
    gen = Generations()  # plain RawPlayer: no get_queue / play_queue / set_play_mode
    a = make(store, settings, gen)
    await a.connect()
    await heos_wait(lambda: a.status().state == "connected")
    with pytest.raises(UnsupportedCommandError):
        await a.get_queue("heos-1")
    with pytest.raises(UnsupportedCommandError):
        await a.play_queue_index("heos-1", 0)
    with pytest.raises(UnsupportedCommandError):
        await a.set_play_mode("heos-1", PlayMode())
    assert (await a.get_play_mode("heos-1")).model_dump() == {"shuffle": False, "repeat": "off"}
    await a.disconnect()


# --------------------------------------------------------------------------------------
# Sonos
# --------------------------------------------------------------------------------------


def test_sonos_play_mode_mapping_round_trips() -> None:
    for name in (
        "NORMAL",
        "SHUFFLE_NOREPEAT",
        "SHUFFLE",
        "REPEAT_ALL",
        "SHUFFLE_REPEAT_ONE",
        "REPEAT_ONE",
    ):
        mode = play_mode_from_sonos(name)
        assert mode is not None and play_mode_to_sonos(mode) == name
    assert play_mode_from_sonos("shuffle") is not None  # case-insensitive
    assert play_mode_from_sonos(None) is None and play_mode_from_sonos("WEIRD") is None


class Res:
    def __init__(self, uri: str, duration: str) -> None:
        self.uri, self.duration = uri, duration


class DidlTrack:
    def __init__(self, i: int) -> None:
        self.title = f"Track {i}"
        self.creator = "Analog Heart"
        self.album = "Warm Glow"
        self.album_art_uri = "/getaa?u=x"
        self.resources = [Res(f"x-sonos-http:track/{10100 + i}.flac?sid=174&sn=3", "0:03:20")]


class QueueResult(list):
    total_matches = 12


async def test_sonos_queue_read_jump_and_play_mode_run_in_a_thread(
    store: StateStore, settings: Settings
) -> None:
    h = Harness()
    a = h.adapter(store, settings)
    await a.connect()
    await heos_wait(lambda: a.status().state == "connected")
    k = h.zones[0]
    k._mode = "NORMAL"  # type: ignore[attr-defined]

    def get_queue(start=0, max_items=100, full_album_art_uri=False):
        k._rec("get_queue", (start, max_items))
        return QueueResult(DidlTrack(i) for i in range(min(max_items, 12)))

    k.get_queue = get_queue  # type: ignore[attr-defined]
    k.play_from_queue = lambda i, start=True: k._rec("play_from_queue", i)  # type: ignore[attr-defined]
    type(k).play_mode = property(  # type: ignore[attr-defined]
        lambda self: (self._guard(), self.__dict__.get("_mode", "NORMAL"))[1],
        lambda self, v: (self._rec("play_mode", v), self.__dict__.__setitem__("_mode", v)),
    )
    try:
        entries, total = await a.get_queue("sonos-RINCON_K", limit=5)
        assert total == 12 and len(entries) == 5
        assert entries[0].title == "Track 0" and entries[0].duration_ms == 200_000
        assert entries[0].content_ref is not None and entries[0].content_ref.id == "10100"
        assert entries[0].art.url is not None  # relative getaa URL was absolutized and registered
        await a.play_queue_index("sonos-RINCON_K", 4)
        assert ("play_from_queue", 4) in k.calls
        assert (await a.get_play_mode("sonos-RINCON_K")).model_dump() == {
            "shuffle": False,
            "repeat": "off",
        }
        await a.set_play_mode("sonos-RINCON_K", PlayMode(shuffle=True, repeat="all"))
        assert ("play_mode", "SHUFFLE") in k.calls
        assert store.state.players["sonos-RINCON_K"].play_mode.shuffle is True
    finally:
        del type(k).play_mode  # type: ignore[attr-defined]
        await a.disconnect()


async def test_sonos_avtransport_event_updates_play_mode_and_schedules_queue_reread(
    store: StateStore, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sonos_mod, "QUEUE_DEBOUNCE_S", 0.02)
    h = Harness()
    a = h.adapter(store, settings)
    await a.connect()
    await heos_wait(lambda: a.status().state == "connected")
    k = h.zones[0]
    k.get_queue = lambda start=0, max_items=100, full_album_art_uri=False: QueueResult(  # type: ignore[attr-defined]
        DidlTrack(i) for i in range(2)
    )
    sub = h.sub("RINCON_K", "avTransport")
    sub.callback(Event({"current_play_mode": "REPEAT_ONE", "number_of_tracks": "2"}))
    assert store.state.players["sonos-RINCON_K"].play_mode.repeat == "one"
    side = "sonos:sonos-gG1"
    await heos_wait(lambda: side in store.state.queues)
    assert [e.title for e in store.state.queues[side].items] == ["Track 0", "Track 1"]
    await a.disconnect()


async def test_heos_queue_refs_follow_the_playing_source_not_a_hard_coded_tidal(
    store: StateStore, settings: Settings
) -> None:
    gen = Generations()
    p = QueuePlayer("Living Room Amp", 1, queue=big_queue(2))
    p.now_playing_media.source_id = 1  # Pandora is playing: queue items are not Tidal tracks
    gen.players = {1: p}
    a = make(store, settings, gen)
    await a.connect()
    await heos_wait(lambda: a.status().state == "connected")
    entries, _total = await a.get_queue("heos-1")
    assert entries[0].content_ref is None and entries[0].art.url is not None
    p.now_playing_media.source_id = None
    entries, _total = await a.get_queue("heos-1")
    assert entries[0].content_ref is None  # unknown source: no canonical ref invented
    await a.disconnect()


async def test_heos_queue_changed_invalidates_cached_queue_ids(
    store: StateStore, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(heos_mod, "QUEUE_DEBOUNCE_S", 5.0)  # keep the re-read out of the way
    gen = Generations()
    p = QueuePlayer("Living Room Amp", 1, queue=[QueueItem(7, "A"), QueueItem(9, "B")])
    gen.players = {1: p}
    a = make(store, settings, gen)
    await a.connect()
    await heos_wait(lambda: a.status().state == "connected")
    await a.get_queue("heos-1")
    assert a._queue_ids["heos-1"] == [7, 9]  # noqa: SLF001
    p.fire(heos_mod.EVENT_QUEUE_CHANGED)
    assert "heos-1" not in a._queue_ids  # noqa: SLF001
    p.queue = [QueueItem(21, "C"), QueueItem(23, "D")]
    await a.play_queue_index("heos-1", 1)  # re-reads: uses the fresh id, not the stale 9
    assert ("play_queue", 23) in p.calls
    await a.disconnect()


async def test_sonos_current_track_event_moves_the_pointer_without_a_reread(
    store: StateStore, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sonos_mod, "QUEUE_DEBOUNCE_S", 0.02)
    h = Harness()
    a = h.adapter(store, settings)
    await a.connect()
    await heos_wait(lambda: a.status().state == "connected")
    k = h.zones[0]
    reads = {"n": 0}

    def get_queue(start=0, max_items=100, full_album_art_uri=False):
        reads["n"] += 1
        return QueueResult(DidlTrack(i) for i in range(3))

    k.get_queue = get_queue  # type: ignore[attr-defined]
    sub = h.sub("RINCON_K", "avTransport")
    side = "sonos:sonos-gG1"
    sub.callback(Event({"number_of_tracks": "3"}))
    await heos_wait(lambda: side in store.state.queues)
    assert reads["n"] == 1
    sub.callback(Event({"current_track": "3"}))  # 1-based pointer
    await asyncio.sleep(0.05)
    assert store.state.queues[side].current_index == 2 and reads["n"] == 1  # no re-read
    sub.callback(Event({"current_track": "garbage"}))
    assert store.state.queues[side].current_index == 2
    await a.disconnect()


async def test_sonos_jump_bounds_and_upnp_refusal_map_to_index_error(
    store: StateStore, settings: Settings
) -> None:
    h = Harness()
    a = h.adapter(store, settings)
    await a.connect()
    await heos_wait(lambda: a.status().state == "connected")
    k = h.zones[0]
    type(k).queue_size = property(lambda self: (self._guard(), 3)[1])  # type: ignore[attr-defined]

    class SoCoUPnPError(Exception):
        pass

    def play_from_queue(i, start=True):
        k._rec("play_from_queue", i)
        if i == 1:
            raise SoCoUPnPError("UPnP Error 711 received")

    k.play_from_queue = play_from_queue  # type: ignore[attr-defined]
    try:
        with pytest.raises(IndexError):
            await a.play_queue_index("sonos-RINCON_K", 5)  # beyond queue_size: never sent
        assert ("play_from_queue", 5) not in getattr(k, "calls", [])
        with pytest.raises(IndexError):
            await a.play_queue_index("sonos-RINCON_K", 1)  # the player refused: mapped
        await a.play_queue_index("sonos-RINCON_K", 2)
        assert ("play_from_queue", 2) in k.calls
    finally:
        del type(k).queue_size  # type: ignore[attr-defined]
        await a.disconnect()


async def test_a_station_playing_over_a_queue_does_not_label_the_queue(
    store: StateStore, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A HEOS player keeps its queue while a station plays over the top.

    Observed on an AVR-X3400H: a 674-track Tidal queue sitting behind a playing Pandora station.
    The station's source describes what is playing, not what is in the queue, so borrowing it
    labelled the whole queue "pandora" and would have badged Tidal tracks with the wrong service.
    """
    monkeypatch.setattr(heos_mod, "QUEUE_DEBOUNCE_S", 0.02)
    gen = Generations()
    p = QueuePlayer("Living Room Amp", 1, queue=big_queue(3))
    p.now_playing_media.source_id = 1  # Pandora
    p.now_playing_media.type = "station"
    p.now_playing_media.media_id = "150519845455144797"  # a station id, not a queue track
    gen.players = {1: p}
    a = make(store, settings, gen)
    await a.connect()
    await heos_wait(lambda: a.status().state == "connected")

    entries, _total = await a.get_queue("heos-1")
    # No canonical ref invented from a station's service.
    assert entries[0].content_ref is None
    assert entries[0].title == "Song 1"  # the items themselves still read fine

    # And the queue-level source stays unset, because the playing track is not in the queue.
    p.fire(heos_mod.EVENT_QUEUE_CHANGED)
    await heos_wait(lambda: "heos:heos-1" in store.state.queues)
    q = store.state.queues["heos:heos-1"]
    assert q.source is None, f"queue mislabelled as {q.source!r}"
    assert q.current_index is None  # the station is not a queue position
    await a.disconnect()
