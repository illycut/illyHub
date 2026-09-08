from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any

import pytest

from illyhub_hub.adapters import sonos as sonos_mod
from illyhub_hub.adapters.sonos import (
    GroupSnapshot,
    SoCoAdapter,
    Topology,
    ZoneSnapshot,
    _hms_to_ms,
    absolutize,
    player_id,
    snapshot_zones,
    source_from_uri,
)
from illyhub_hub.art import placeholder_ref
from illyhub_hub.config import Settings
from illyhub_hub.state import StateStore


class LazyZone:
    """Behaves like a SoCo object: every property does I/O and must run off the loop thread."""

    def __init__(self, uid: str, name: str, ip: str, group: LazyGroup | None = None, visible=True):
        self._uid, self._name, self._ip, self._group, self._visible = uid, name, ip, group, visible

    @staticmethod
    def _guard() -> None:
        assert threading.current_thread() is not threading.main_thread(), "SOAP on loop thread"

    @property
    def uid(self):
        self._guard()
        return self._uid

    @property
    def player_name(self):
        self._guard()
        return self._name

    @property
    def ip_address(self):
        self._guard()
        return self._ip

    @property
    def is_visible(self):
        self._guard()
        return self._visible

    @property
    def model_name(self):
        self._guard()
        return "Sonos One"

    @property
    def volume(self):
        self._guard()
        return 27

    @property
    def mute(self):
        self._guard()
        return False

    def get_current_transport_info(self):
        self._guard()
        return {"current_transport_state": "PLAYING"}

    # -- SoCo command surface: every call must run off the loop thread -------------------
    calls: list[tuple[str, Any]]
    position: str = "0:00:00"

    def _rec(self, name: str, arg: Any = None) -> None:
        self._guard()
        self.__dict__.setdefault("calls", []).append((name, arg))

    def play(self):
        self._rec("play")

    def pause(self):
        self._rec("pause")

    def stop(self):
        self._rec("stop")

    def next(self):
        self._rec("next")

    def previous(self):
        self._rec("previous")

    def seek(self, position):
        self._rec("seek", position)
        self.position = position

    @volume.setter
    def volume(self, level):
        self._rec("volume", level)

    @mute.setter
    def mute(self, muted):
        self._rec("mute", muted)

    def join(self, master):
        self._rec("join", master._uid)

    def unjoin(self):
        self._rec("unjoin")

    def get_current_track_info(self):
        self._guard()
        self.__dict__.setdefault("track_info_calls", 0)
        self.__dict__["track_info_calls"] += 1
        return {"position": self.position, "duration": "0:03:33"}

    @property
    def group(self):
        self._guard()
        return self._group


class LazyGroup:
    def __init__(self, uid: str, coordinator: LazyZone, members: list[LazyZone]):
        self._uid, self._coordinator, self._members = uid, coordinator, members

    @property
    def uid(self):
        LazyZone._guard()
        return self._uid

    @property
    def coordinator(self):
        LazyZone._guard()
        return self._coordinator

    @property
    def members(self):
        LazyZone._guard()
        return self._members

    @property
    def label(self):
        LazyZone._guard()
        return "Kitchen + Patio"


def lazy_topology() -> list[LazyZone]:
    k = LazyZone("RINCON_K", "Kitchen", "192.168.1.31")
    p = LazyZone("RINCON_P", "Patio", "192.168.1.32")
    h = LazyZone("RINCON_H", "Hidden", "192.168.1.33", visible=False)
    g = LazyGroup("G1", k, [k, p])
    k._group = p._group = g
    return [k, p, h]


def test_snapshot_zones_must_run_off_the_loop_thread() -> None:
    with pytest.raises(AssertionError, match="SOAP on loop thread"):
        snapshot_zones(lazy_topology())


async def test_snapshot_zones_in_thread_captures_everything_once() -> None:
    topo = await asyncio.to_thread(snapshot_zones, lazy_topology())
    assert [z.uid for z in topo.zones] == ["RINCON_K", "RINCON_P"]  # hidden dropped
    assert topo.zones[0] == ZoneSnapshot(
        "RINCON_K", "Kitchen", "192.168.1.31", "Sonos One", True, 27, False, "PLAYING"
    )
    assert topo.groups == [
        GroupSnapshot("G1", "RINCON_K", ("RINCON_K", "RINCON_P"), "Kitchen + Patio")
    ]
    assert set(topo.raw) == {"RINCON_K", "RINCON_P"}


@dataclass
class Sub:
    zone: Any
    service: str
    callback: Any
    auto_renew_fail: Any = None
    unsubscribed: bool = False

    async def unsubscribe(self, strict: bool = True) -> None:
        self.unsubscribed = True


@dataclass
class Event:
    variables: dict[str, Any] = field(default_factory=dict)


class Harness:
    """Feeds the adapter snapshots built in a worker thread from LazyZone objects."""

    def __init__(self, empty: bool = False, fail_first: bool = False) -> None:
        self.zones = [] if empty else lazy_topology()
        self.subs: list[Sub] = []
        self.fail_first = fail_first
        self.snapshot_calls = 0
        self.groups_override: list[GroupSnapshot] | None = None

    async def snapshot(self) -> Topology:
        self.snapshot_calls += 1
        if self.fail_first:
            self.fail_first = False
            raise OSError("no multicast")
        return await asyncio.to_thread(snapshot_zones, self.zones)

    async def groups(self, raw: Any) -> list[GroupSnapshot]:
        if self.groups_override is not None:
            return self.groups_override
        LazyZone._guard()  # the adapter must not call this on the loop thread either
        return []  # pragma: no cover

    async def subscribe(self, raw: Any, service: str, callback: Any) -> Sub:
        sub = Sub(raw, service, callback)
        self.subs.append(sub)
        return sub

    def sub(self, uid: str, service: str) -> Sub:
        return next(s for s in self.subs if s.zone._uid == uid and s.service == service)

    def adapter(self, store: StateStore, settings: Settings) -> SoCoAdapter:
        return SoCoAdapter(
            store, settings, snapshot=self.snapshot, groups=self.groups, subscribe=self.subscribe
        )


async def wait_for(pred, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.005)


async def test_connect_uses_snapshots_seeds_state_and_subscribes_with_callbacks(
    store: StateStore, settings: Settings
) -> None:
    h = Harness()
    a = h.adapter(store, settings)
    await a.connect()
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    k = store.state.players["sonos-RINCON_K"]
    assert k.volume == 27 and k.play_state == "play" and k.model == "Sonos One"  # seeded in thread
    assert k.capabilities.supports_power is False
    side = store.state.sides["sonos:sonos-gG1"]
    assert side.coordinator_player_id == "sonos-RINCON_K" and side.name == "Kitchen + Patio"
    assert sorted((s.zone._uid, s.service) for s in h.subs) == [
        ("RINCON_K", "avTransport"),
        ("RINCON_K", "renderingControl"),
        ("RINCON_K", "zoneGroupTopology"),
        ("RINCON_P", "avTransport"),
        ("RINCON_P", "renderingControl"),
    ]
    assert all(callable(s.callback) for s in h.subs)  # callback handed in at subscribe time
    await a.disconnect()
    assert all(s.unsubscribed for s in h.subs) and a.status().state == "disconnected"


async def test_events_update_state_absolutize_art_and_contain_errors(
    store: StateStore, settings: Settings
) -> None:
    h = Harness()
    a = h.adapter(store, settings)
    kinds: list[str] = []
    a.on_event(lambda e: kinds.append(e.kind))
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    h.sub("RINCON_P", "renderingControl").callback(
        Event({"volume": {"Master": "33"}, "mute": {"Master": "1"}})
    )
    p = store.state.players["sonos-RINCON_P"]
    assert p.volume == 33 and p.muted is True
    h.sub("RINCON_K", "avTransport").callback(
        Event(
            {
                "transport_state": "PAUSED_PLAYBACK",
                "current_track_duration": "0:03:33",
                "current_track_uri": "x-sonos-http:track/1.flac?sid=174&flags=8224&sn=3",
                "current_track_meta_data": {
                    "title": "Signal",
                    "creator": "Analog Heart",
                    "album": "Warm Glow",
                    "album_art_uri": "/getaa?s=1&u=x",
                    "item_class": "object.item.audioItem.musicTrack",
                },
            }
        )
    )
    assert store.state.players["sonos-RINCON_K"].play_state == "pause"
    np = store.state.now_playing["sonos:sonos-gG1"]
    assert np.title == "Signal" and np.duration_ms == 213000 and np.seekable is True
    assert np.art == placeholder_ref()  # no ArtCache wired: hub-relative placeholder
    assert np.source == "tidal" and np.track_id.startswith("x-sonos-http:track/1.flac")
    h.sub("RINCON_K", "avTransport").callback(
        Event(
            {
                "current_track_meta_data": {
                    "title": "Radio",
                    "item_class": "object.item.audioItem.audioBroadcast",
                }
            }
        )
    )
    assert store.state.now_playing["sonos:sonos-gG1"].seekable is False
    h.sub("RINCON_P", "renderingControl").callback(
        Event({"volume": {"Master": "loud"}})
    )  # contained
    assert store.state.players["sonos-RINCON_P"].volume == 33
    assert {"connected", "volume", "play_state", "now_playing"} <= set(kinds)
    await a.disconnect()


async def test_topology_event_regroups_via_thread_snapshot(
    store: StateStore, settings: Settings
) -> None:
    h = Harness()
    a = h.adapter(store, settings)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    h.groups_override = []  # everyone ungrouped
    h.sub("RINCON_K", "zoneGroupTopology").callback(Event({}))
    await wait_for(lambda: "sonos-gG1" not in store.state.groups)
    assert set(store.state.sides) == {"sonos:sonos-RINCON_K", "sonos:sonos-RINCON_P"}

    async def boom(raw):
        raise RuntimeError("soap")

    a._groups = boom  # type: ignore[assignment]
    h.sub("RINCON_K", "zoneGroupTopology").callback(Event({}))
    await asyncio.sleep(0.02)
    assert a.status().state == "connected"
    await a.disconnect()


async def test_renewal_failure_marks_offline_resubscribes_and_backoff_holds(
    store: StateStore, settings: Settings
) -> None:
    h = Harness(fail_first=True)
    a = h.adapter(store, settings)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected", timeout=2)
    assert h.snapshot_calls == 2 and a._backoff.attempt == 1  # not reset: held < 30s
    first_subs = list(h.subs)
    h.sub("RINCON_K", "avTransport").auto_renew_fail(TimeoutError("renew"))
    await wait_for(lambda: store.state.connections["sonos"].state == "reconnecting", timeout=2)
    await wait_for(lambda: len(h.subs) > len(first_subs), timeout=2)
    await wait_for(lambda: a.status().state == "connected", timeout=2)
    assert all(s.unsubscribed for s in first_subs)
    assert store.state.players["sonos-RINCON_K"].online is True
    await a.disconnect()


async def test_no_players_is_a_connect_error(store: StateStore, settings: Settings) -> None:
    h = Harness(empty=True)
    a = h.adapter(store, settings)
    await a.connect()
    await wait_for(
        lambda: (
            store.state.connections.get("sonos", None) is not None
            and store.state.connections["sonos"].state == "reconnecting"
        )
    )
    assert "no Sonos players" in (store.state.connections["sonos"].last_error or "")
    await a.disconnect()


def test_helpers() -> None:
    assert player_id("RINCON_X") == "sonos-RINCON_X"
    assert _hms_to_ms("0:03:33") == 213000 and _hms_to_ms("NOT_IMPLEMENTED") is None
    assert absolutize("/getaa?x", "1.2.3.4") == "http://1.2.3.4:1400/getaa?x"
    assert absolutize("getaa?x", "1.2.3.4") == "http://1.2.3.4:1400/getaa?x"
    assert absolutize("http://cdn/a.jpg", "1.2.3.4") == "http://cdn/a.jpg"
    assert absolutize(None, "1.2.3.4") is None and absolutize("/x", None) == "/x"
    assert source_from_uri("x-sonos-http:a?sid=236&sn=1") == "pandora"
    assert source_from_uri("x-sonos-http:a?sid=999") is None and source_from_uri(None) is None


async def test_commands_run_soco_methods_in_a_thread(store: StateStore, settings: Settings) -> None:
    h = Harness()
    a = h.adapter(store, settings)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    k, p = "sonos-RINCON_K", "sonos-RINCON_P"
    await a.play(k)
    await a.pause(k)
    await a.stop(k)
    await a.next(k)
    await a.previous(k)
    await a.seek(k, 95_500)
    assert store.state.positions["sonos:sonos-gG1"].position_ms == 95_500  # optimistic write
    assert store.state.positions["sonos:sonos-gG1"].confidence == 0.5
    await a.set_volume(p, 41)
    await a.set_mute(p, True)
    kz = next(z for z in h.zones if z._uid == "RINCON_K")
    pz = next(z for z in h.zones if z._uid == "RINCON_P")
    # Reconcile against the current topology (K+P already grouped): nothing to do ...
    await a.set_group([k, p])
    assert not [c for c in pz.calls if c[0] in ("join", "unjoin")]
    # ... a single id removes that player from its group ...
    await a.set_group([k])
    assert kz.calls[-1] == ("unjoin", None)
    # ... a desired list without P makes P (the leaver) unjoin, K stays ...
    kz_before = len(kz.calls)
    await a.set_group([k, k])
    assert pz.calls[-1] == ("unjoin", None) and len(kz.calls) == kz_before
    store.set_groups("sonos", [])  # the topology event would do this; simulate it
    # ... a newcomer joins the coordinator; dissolve unjoins non-coordinators only.
    await a.set_group([k, p])
    assert pz.calls[-1] == ("join", "RINCON_K")
    kz_before = len(kz.calls)
    await a.dissolve(k, [k, p])
    assert pz.calls[-1] == ("unjoin", None) and len(kz.calls) == kz_before
    assert kz.calls[:6] == [
        ("play", None),
        ("pause", None),
        ("stop", None),
        ("next", None),
        ("previous", None),
        ("seek", "0:01:35"),
    ]
    with pytest.raises(ConnectionError):
        await a.play("sonos-RINCON_NOPE")
    with pytest.raises(ValueError):
        await a.set_group([])
    assert sonos_mod.ms_to_hms(3_725_000) == "1:02:05" and sonos_mod.ms_to_hms(-5) == "0:00:00"
    await a.disconnect()


async def test_position_poller_runs_only_while_playing_and_converges(
    store: StateStore, settings: Settings
) -> None:
    class Clock:
        t = 1000.0

        def __call__(self) -> float:
            return self.t

    clock = Clock()
    h = Harness()
    a = SoCoAdapter(
        store,
        settings,
        snapshot=h.snapshot,
        groups=h.groups,
        subscribe=h.subscribe,
        clock=clock,
    )
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    side = "sonos:sonos-gG1"
    kz = next(z for z in h.zones if z._uid == "RINCON_K")
    assert side in a._pollers  # coordinator seeded as PLAYING -> poller started
    # Drive polls by hand on the fake clock: the device counter ticks at anchor 999.6 and
    # reports floor seconds; the tracker's bisection schedule must converge.
    anchor = 999.6
    for _ in range(6):
        kz.position = sonos_mod.ms_to_hms(int((clock.t - anchor) * 1000))
        await a._poll_once(side, "sonos-RINCON_K")
        clock.t += a._trackers[side].next_poll_in(clock.t, 1.0)
    kz.position = sonos_mod.ms_to_hms(int((clock.t - anchor) * 1000))
    await a._poll_once(side, "sonos-RINCON_K")
    pos = store.state.positions[side]
    assert pos.confidence == 0.9
    assert abs(pos.position_ms - (clock.t - anchor) * 1000) < 150
    # Pause the coordinator: poller stops, estimate frozen.
    h.sub("RINCON_K", "avTransport").callback(Event({"transport_state": "PAUSED_PLAYBACK"}))
    await wait_for(lambda: side not in a._pollers)
    frozen = store.state.positions[side].position_ms
    calls = kz.__dict__.get("track_info_calls", 0)
    await asyncio.sleep(settings.position_poll_s * 3)
    assert kz.__dict__.get("track_info_calls", 0) == calls  # no polling while paused
    assert store.state.positions[side].position_ms == frozen
    # Track change resets the tracker; seek from the hub does too.
    h.sub("RINCON_K", "avTransport").callback(
        Event({"current_track_meta_data": {"title": "New", "uri": "x-sonos:2"}})
    )
    assert a._trackers[side].observations == 0
    h.sub("RINCON_K", "avTransport").callback(Event({"transport_state": "PLAYING"}))
    await wait_for(lambda: side in a._pollers)
    await a.seek("sonos-RINCON_K", 5000)
    assert a._trackers[side].observations == 0
    await a.disconnect()
    assert not a._pollers


async def test_poll_errors_are_swallowed(store: StateStore, settings: Settings) -> None:
    h = Harness()
    a = h.adapter(store, settings)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    kz = next(z for z in h.zones if z._uid == "RINCON_K")

    def boom():
        raise OSError("timeout")

    kz.get_current_track_info = boom  # type: ignore[method-assign]
    await asyncio.sleep(settings.position_poll_s * 3)
    assert "sonos:sonos-gG1" in a._pollers  # still polling
    await a.disconnect()


async def test_topology_event_resnapshots_with_throttle(
    store: StateStore, settings: Settings
) -> None:
    class Clock:
        t = 0.0

        def __call__(self) -> float:
            return self.t

    clock = Clock()
    h = Harness()
    a = SoCoAdapter(
        store, settings, snapshot=h.snapshot, groups=h.groups, subscribe=h.subscribe, clock=clock
    )
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    assert h.snapshot_calls == 1
    # A new player appears; the topology event arrives 3 s later -> full re-snapshot.
    h.zones.append(LazyZone("RINCON_D", "Den", "192.168.1.40"))
    clock.t = 3.0
    h.sub("RINCON_K", "zoneGroupTopology").callback(Event({}))
    await wait_for(lambda: "sonos-RINCON_D" in store.state.players)
    assert h.snapshot_calls == 2
    assert {s.service for s in h.subs if s.zone._uid == "RINCON_D"} == {
        "avTransport",
        "renderingControl",
    }
    # Another event right away is throttled to the cheap group re-read.
    h.groups_override = []
    h.sub("RINCON_K", "zoneGroupTopology").callback(Event({}))
    await wait_for(lambda: store.state.players["sonos-RINCON_P"].group_id is None)
    assert h.snapshot_calls == 2
    # Track change writes an optimistic zero position.
    h.sub("RINCON_K", "avTransport").callback(
        Event(
            {
                "current_track_meta_data": {
                    "title": "New",
                    "uri": "x-sonos:9",
                    "item_class": "object.item.audioItem.audioBroadcast",
                }
            }
        )
    )
    pos = store.state.positions["sonos:sonos-RINCON_K"]
    assert pos.position_ms == 0 and pos.confidence == 0.5
    np = store.state.now_playing["sonos:sonos-RINCON_K"]
    assert np.supports_next is False and np.seekable is False
    await a.disconnect()


async def test_relative_album_art_is_absolutized_before_registration(
    store: StateStore, settings: Settings, tmp_path
) -> None:
    """The Sonos ``/getaa?...`` path must be absolutized against the player before the art proxy
    fetches it; the client only ever sees the hub-relative ArtRef."""
    from illyhub_hub.art import ArtCache, ArtHelper

    cache = ArtCache(tmp_path / "art")
    h = Harness()
    a = SoCoAdapter(
        store,
        settings,
        snapshot=h.snapshot,
        groups=h.groups,
        subscribe=h.subscribe,
        art=ArtHelper(cache),
    )
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    h.sub("RINCON_K", "avTransport").callback(
        Event(
            {
                "transport_state": "PLAYING",
                "current_track_uri": "x-sonos-http:track/1.flac?sid=174",
                "current_track_meta_data": {
                    "title": "Signal",
                    "creator": "Analog Heart",
                    "album_art_uri": "/getaa?s=1&u=x",
                },
            }
        )
    )
    np = store.state.now_playing["sonos:sonos-gG1"]
    assert np.art.url == f"/api/art/{np.art.cache_key}" and np.art.cache_key
    meta = cache._read_meta(np.art.cache_key)
    assert meta.source_url == "http://192.168.1.31:1400/getaa?s=1&u=x"
    await a.disconnect()


# -- Phase 3: Tidal refs and play by canonical id -------------------------------------------


def test_tidal_ref_builders_match_the_spike_doc() -> None:
    from illyhub_hub.adapters.base import PlayableTrack
    from illyhub_hub.adapters.sonos import (
        TIDAL_DESC,
        build_tidal_didl,
        tidal_item_id,
        tidal_parent_id,
        tidal_track_uri,
    )

    assert tidal_track_uri("10101", "7") == "x-sonos-http:track/10101.flac?sid=174&flags=8224&sn=7"
    assert tidal_item_id("10101") == "10032020track/10101"
    assert tidal_parent_id("101", None) == "1004206calbum/101"
    assert tidal_parent_id("101", "p-1") == "1006006cplaylist/p-1"
    assert tidal_parent_id(None, None) == "10032020"
    assert TIDAL_DESC == "SA_RINCON44551_X_#Svc44551-0-Token"
    track = PlayableTrack(
        service="tidal",
        track_id="10101",
        title="Signal",
        artist="Analog Heart",
        album="Warm Glow",
        album_id="101",
    )
    didl = build_tidal_didl(track, "7")
    xml = didl.to_element()
    from xml.etree import ElementTree as ET

    text = ET.tostring(xml).decode()
    assert "x-sonos-http:track/10101.flac?sid=174&amp;flags=8224&amp;sn=7" in text
    assert 'id="10032020track/10101"' in text and 'parentID="1004206calbum/101"' in text
    assert (
        "SA_RINCON44551_X_#Svc44551-0-Token" in text and "object.item.audioItem.musicTrack" in text
    )


async def test_play_content_clears_queue_batches_and_plays_from_index(
    store: StateStore, settings: Settings
) -> None:
    from illyhub_hub.adapters.base import ContentUnavailableError, PlayableTrack
    from illyhub_hub.content import ContentRef

    h = Harness()
    a = h.adapter(store, settings)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    k = h.zones[0]
    k.clear_queue = lambda: k._rec("clear_queue")  # type: ignore[attr-defined]
    k.add_multiple_to_queue = lambda items: k._rec("add_multiple", len(items))  # type: ignore[attr-defined]
    k.play_from_queue = lambda i: k._rec("play_from_queue", i)  # type: ignore[attr-defined]
    tracks = [
        PlayableTrack(service="tidal", track_id=str(i), title=f"T{i}", album_id="101")
        for i in range(20)
    ]
    ref = ContentRef(service="tidal", kind="album", id="101")
    assert a.service_linked("tidal") is False  # no sn discovered, no setting
    with pytest.raises(ContentUnavailableError):
        await a.play_content("sonos-RINCON_K", ref, tracks, 0)
    a.settings = settings.model_copy(update={"sonos_tidal_sn": "3"})
    assert a.service_linked("tidal") is True
    await a.play_content("sonos-RINCON_K", ref, tracks, start_index=5)
    names = [c for c in k.calls if c[0] in {"clear_queue", "add_multiple", "play_from_queue"}]
    assert names == [("clear_queue", None), ("add_multiple", 20), ("play_from_queue", 5)]
    side = "sonos:sonos-gG1"
    assert store.state.positions[side].position_ms == 0
    assert store.state.positions[side].confidence == 0.5
    with pytest.raises(ContentUnavailableError):
        await a.play_content(
            "sonos-RINCON_K", ContentRef(service="ytmusic", kind="album", id="x"), tracks, 0
        )
    await a.disconnect()


def test_discover_tidal_sn_maps_service_type_44551() -> None:
    from illyhub_hub.adapters.sonos import discover_tidal_sn

    class Acct:
        def __init__(self, service_type: str) -> None:
            self.service_type = service_type

    class Accounts:
        @staticmethod
        def get_accounts(zone):
            assert zone == "zone"
            return {"1": Acct("2311"), "7": Acct("44551"), "9": Acct("65031")}

    class Broken:
        @staticmethod
        def get_accounts(zone):
            raise OSError("no route")

    class NoTidal:
        @staticmethod
        def get_accounts(zone):
            return {"1": Acct("2311")}

    assert discover_tidal_sn("zone", account_class=Accounts) == "7"
    assert discover_tidal_sn("zone", account_class=NoTidal) is None
    assert discover_tidal_sn("zone", account_class=Broken) is None
    assert discover_tidal_sn(None, account_class=Accounts) is None


async def test_prime_content_positions_queue_without_playing(
    store: StateStore, settings: Settings
) -> None:
    """Sync Play priming on Sonos: clear → add → play_from_queue(index, start=False) → pause;
    start_primed → play."""
    from illyhub_hub.adapters.base import ContentUnavailableError, PlayableTrack
    from illyhub_hub.content import ContentRef

    h = Harness()
    a = h.adapter(store, settings)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    k = h.zones[0]

    class Queued:
        title = "T5"

    k.clear_queue = lambda: k._rec("clear_queue")  # type: ignore[attr-defined]
    k.add_multiple_to_queue = lambda items: k._rec("add_multiple", len(items))  # type: ignore[attr-defined]
    k.play_from_queue = lambda i, start=True: k._rec("play_from_queue", (i, start))  # type: ignore[attr-defined]
    k.pause = lambda: k._rec("pause")  # type: ignore[attr-defined]
    k.play = lambda: k._rec("play")  # type: ignore[attr-defined]
    k.get_queue = lambda start, n: (k._rec("get_queue", (start, n)), [Queued()])[1]  # type: ignore[attr-defined]
    tracks = [
        PlayableTrack(service="tidal", track_id=str(i), title=f"T{i}", album_id="101")
        for i in range(8)
    ]
    ref = ContentRef(service="tidal", kind="album", id="101")
    with pytest.raises(ContentUnavailableError):
        await a.prime_content("sonos-RINCON_K", ref, tracks, 0)  # no sn
    a.settings = settings.model_copy(update={"sonos_tidal_sn": "3"})
    primed = await a.prime_content("sonos-RINCON_K", ref, tracks, start_index=5)
    assert primed.track_id == "5" and primed.title == "T5"
    names = [
        c
        for c in k.calls
        if c[0] in {"clear_queue", "add_multiple", "play_from_queue", "pause", "get_queue", "play"}
    ]
    assert names == [
        ("clear_queue", None),
        ("add_multiple", 8),
        ("play_from_queue", (5, False)),
        ("pause", None),
        ("get_queue", (5, 1)),
    ]
    assert store.state.positions["sonos:sonos-gG1"].position_ms == 0
    await a.start_primed("sonos-RINCON_K", 5)
    assert k.calls[-1] == ("play", None)
    with pytest.raises(IndexError):
        await a.prime_content("sonos-RINCON_K", ref, tracks, 99)
    with pytest.raises(ContentUnavailableError):
        await a.prime_content(
            "sonos-RINCON_K", ContentRef(service="ytmusic", kind="album", id="x"), tracks, 0
        )
    await a.disconnect()


# --------------------------------------------------------------------------------------
# Phase 5: Pandora stations via SMAPI (SoCo MusicService) + programmed-radio URIs
# --------------------------------------------------------------------------------------


def test_discover_pandora_sn_maps_service_type_60423() -> None:
    from illyhub_hub.adapters.sonos import PANDORA_SERVICE_TYPE, discover_pandora_sn

    assert PANDORA_SERVICE_TYPE == "60423"

    class Acct:
        def __init__(self, service_type: str) -> None:
            self.service_type = service_type

    class Accounts:
        @staticmethod
        def get_accounts(zone):
            return {"1": Acct("2311"), "4": Acct("60423"), "7": Acct("44551")}

    assert discover_pandora_sn("zone", account_class=Accounts) == "4"
    assert discover_pandora_sn(None, account_class=Accounts) is None


def test_station_uri_and_didl_helpers() -> None:
    from illyhub_hub.adapters.base import VendorStation
    from illyhub_hub.adapters.sonos import (
        PANDORA_DESC,
        build_station_didl,
        pandora_item_from_uri,
        pandora_item_id,
        pandora_station_uri,
    )

    uri = pandora_station_uri("ST:123 456", "4")
    assert uri == "x-sonosapi-radio:ST%3A123%20456?sid=236&flags=8300&sn=4"
    assert pandora_item_id("ST:1") == "F00092020ST%3A1"
    assert pandora_item_from_uri(uri) == "ST%3A123%20456"
    assert pandora_item_from_uri("x-sonos-http:track/1.flac?sid=174") is None
    assert pandora_item_from_uri(None) is None
    assert PANDORA_DESC == "SA_RINCON60423_X_#Svc60423-0-Token"
    st = VendorStation(vendor="sonos", name="Chill Radio", ids={"id": "ST:1"})
    got_uri, didl = build_station_didl(st, "4")
    assert got_uri == pandora_station_uri("ST:1", "4")
    assert didl.item_class == "object.item.audioItem.audioBroadcast"
    assert didl.title == "Chill Radio" and didl.desc == PANDORA_DESC
    assert didl.resources[0].uri == got_uri


class SmapiService:
    """Just enough ``MusicService`` for SoCo's own ``MusicServiceItem.from_music_service`` to
    build real items (``desc`` and the id→uri helper)."""

    service_id = 236
    desc = "SA_RINCON60423_X_#Svc60423-0-Token"

    def sonos_uri_from_id(self, item_id: str) -> str:  # pragma: no cover - tracks only
        return f"soco://{item_id}?sid=236&sn=0"


def smapi_item(class_key: str, **raw: Any) -> Any:
    """A **real** SoCo music-service item (``MediaCollectionContainer``, ``MediaMetadataProgram``
    …) parsed from the SOAP-shaped dict SoCo receives: fields end up snake_cased in
    ``.metadata`` and booleans coerced, exactly as on hardware."""
    from soco.music_services.data_structures import get_class

    return get_class(class_key).from_music_service(SmapiService(), raw)


def smapi_result(items: list[Any]) -> Any:
    from soco.data_structures import SearchResult

    return SearchResult(items, "browse", len(items), None, None)


class FakeMusicService:
    def __init__(self, name: str, device: Any) -> None:
        LazyZone._guard()
        self.name, self.device = name, device
        self.calls: list[str] = []
        self.fault: Exception | None = None
        self.tree = {
            "root": [
                smapi_item(
                    "MediaCollectionContainer",
                    id="stations",
                    title="My Stations",
                    itemType="container",
                    canEnumerate="true",
                    canPlay="false",
                ),
                smapi_item(
                    "MediaCollectionSearch",
                    id="search",
                    title="Search",
                    itemType="search",
                    canEnumerate="true",
                ),
                smapi_item(
                    "MediaMetadataProgram",
                    id="ST:9",
                    title="Thumbprint Radio",
                    itemType="program",
                    mimeType="audio/mpeg",
                ),
                smapi_item(
                    "MediaCollectionAlbum",
                    id="quick",
                    title="Quick play",
                    itemType="album",
                    canPlay="true",
                    canEnumerate="false",
                    albumArtURI="http://p/quick.jpg",
                ),
            ],
            "stations": [
                smapi_item(
                    "MediaMetadataProgram",
                    id="ST:1",
                    title="Chill Radio",
                    itemType="program",
                    summary="Chill",
                    trackMetadata={"albumArtURI": "http://p/chill.jpg", "artist": "Low Tide"},
                ),
                smapi_item(
                    "MediaMetadataStream",
                    id="ST:2",
                    title="Jazz Nights",
                    itemType="stream",
                    streamMetadata={"logo": "http://p/jazz.jpg", "bitrate": "128"},
                ),
                smapi_item(
                    "MediaMetadataProgram", id="ST:1", title="Chill Radio (dup)", itemType="program"
                ),
                smapi_item(
                    "MediaCollectionContainer",
                    id="deep",
                    title="Folder",
                    itemType="container",
                    canEnumerate="true",
                ),
            ],
            "search": [],
            "deep": [
                smapi_item("MediaMetadataProgram", id="ST:3", title="Too deep", itemType="program")
            ],
        }

    def get_metadata(self, item_id: str, index: int = 0, count: int = 100) -> Any:
        LazyZone._guard()
        self.calls.append(item_id)
        if self.fault is not None:
            raise self.fault
        return smapi_result(self.tree.get(item_id, []))


def test_smapi_field_reads_match_soco_storage() -> None:
    from illyhub_hub.adapters.sonos import _md, _station_art, snake

    assert snake("albumArtURI") == "album_art_uri" and snake("canEnumerate") == "can_enumerate"
    assert snake("itemType") == "item_type" and snake("streamMetadata") == "stream_metadata"
    coll = smapi_item(
        "MediaCollectionContainer",
        id="x",
        title="X",
        itemType="container",
        canEnumerate="true",
        canPlay="false",
        albumArtURI="http://a",
    )
    assert coll.metadata["can_enumerate"] is True  # SoCo coerced the SOAP string
    assert _md(coll, "canEnumerate") is True and _md(coll, "canPlay") is False
    assert _md(coll, "itemType") == "container" and _station_art(coll) == "http://a"
    prog = smapi_item(
        "MediaMetadataProgram",
        id="p",
        title="P",
        itemType="program",
        streamMetadata={"logo": "http://logo"},
    )
    assert _md(prog, "canEnumerate", False) is False  # MediaMetadata has no such field
    assert _station_art(prog) == "http://logo"
    assert _md({"itemType": "stream"}, "itemType") == "stream"
    assert _md({"item_type": "stream"}, "itemType") == "stream"
    assert _md(prog, "nope", "dflt") == "dflt"


def test_collect_smapi_stations_walks_real_items_off_the_loop_thread() -> None:
    from illyhub_hub.adapters.base import ServiceAuthError
    from illyhub_hub.adapters.sonos import collect_smapi_stations

    holder: dict[str, Any] = {}

    def work() -> None:
        svc = FakeMusicService("Pandora", "zone")
        holder["stations"] = collect_smapi_stations(svc)
        holder["calls"] = list(svc.calls)
        from soco.exceptions import MusicServiceAuthException, MusicServiceException

        svc.fault = MusicServiceAuthException("Client.AuthTokenExpired")
        try:
            collect_smapi_stations(svc)
        except ServiceAuthError as exc:
            holder["auth"] = str(exc)
        svc.fault = MusicServiceException("Server.ServiceUnavailable")
        try:
            collect_smapi_stations(svc)
        except RuntimeError as exc:
            holder["soap"] = str(exc)

    t = threading.Thread(target=work)
    t.start()
    t.join()
    stations = holder["stations"]
    # Depth-first; containers entered up to two levels below root; duplicate ids skipped;
    # a playable non-enumerable collection counts as a station.
    assert [s.name for s in stations] == [
        "Chill Radio",
        "Jazz Nights",
        "Too deep",
        "Thumbprint Radio",
        "Quick play",
    ]
    assert stations[0].ids == {"id": "ST:1"} and stations[0].art_url == "http://p/chill.jpg"
    assert stations[0].subtitle == "Chill" and stations[1].art_url == "http://p/jazz.jpg"
    assert stations[4].art_url == "http://p/quick.jpg"
    assert holder["calls"] == ["root", "stations", "deep", "search"]
    assert "signed in again" in holder["auth"] and "browse fault" in holder["soap"]
    with pytest.raises(AssertionError):
        collect_smapi_stations(FakeMusicService.__new__(FakeMusicService))  # loop thread → guard


async def test_list_and_play_station_run_soco_in_a_thread(
    store: StateStore, settings: Settings
) -> None:
    from illyhub_hub.adapters.base import ContentUnavailableError
    from illyhub_hub.adapters.sonos import SoCoAdapter
    from illyhub_hub.content import ContentRef

    h = Harness()
    made: list[FakeMusicService] = []

    def factory(name: str, device: Any) -> FakeMusicService:
        svc = FakeMusicService(name, device)
        made.append(svc)
        return svc

    a = SoCoAdapter(
        store,
        settings,
        snapshot=h.snapshot,
        groups=h.groups,
        subscribe=h.subscribe,
        music_service=factory,
    )
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    assert a.service_linked("pandora") is False
    with pytest.raises(ContentUnavailableError):
        await a.list_stations("pandora")
    with pytest.raises(ContentUnavailableError):
        await a.list_stations("tidal")
    a.settings = settings.model_copy(update={"sonos_pandora_sn": "4"})
    assert a.service_linked("pandora") is True
    stations = await a.list_stations("pandora")
    assert [s.name for s in stations][:3] == ["Chill Radio", "Jazz Nights", "Too deep"]
    assert made and made[0].name == "Pandora" and made[0].device is h.zones[0]
    await a.list_stations("pandora")
    assert len(made) == 1  # the SoCo MusicService (SOAP session) is cached per adapter
    # An auth fault surfaces as ServiceAuthError and drops the cached session.
    from soco.exceptions import MusicServiceAuthException

    from illyhub_hub.adapters.base import ServiceAuthError

    made[0].fault = MusicServiceAuthException("Client.TokenRefreshRequired")
    with pytest.raises(ServiceAuthError):
        await a.list_stations("pandora")
    assert len(made) == 1
    await a.list_stations("pandora")  # a fresh MusicService is built on the next call
    assert len(made) == 2

    k = h.zones[0]
    k.play_uri = lambda uri, meta="", title="", start=True: k._rec(
        "play_uri", (uri, title, start, "audioBroadcast" in meta)
    )  # type: ignore[attr-defined]
    ref = ContentRef(service="pandora", kind="station", id="chill-radio")
    await a.play_station("sonos-RINCON_K", ref, stations[0])
    call = next(c for c in k.calls if c[0] == "play_uri")
    assert call[1] == ("x-sonosapi-radio:ST%3A1?sid=236&flags=8300&sn=4", "Chill Radio", True, True)
    side = "sonos:sonos-gG1"
    np = store.state.now_playing[side]
    assert np.title == "Chill Radio" and np.source == "pandora" and np.content_ref == ref
    assert np.seekable is False and np.supports_prev is False and np.supports_next is True
    assert store.state.positions[side].confidence == 0.5
    assert store.state.players["sonos-RINCON_K"].play_state == "play"  # optimistic

    # A transport event for the station keeps the station identity; another station chosen in
    # the Sonos app (different id in the URI) or a Tidal URI drops it.
    sub = h.sub("RINCON_K", "avTransport")
    sub.callback(
        Event(
            {
                "transport_state": "PLAYING",
                "current_track_uri": "x-sonosapi-radio:ST%3A2?sid=236&flags=8300&sn=4",
                "current_track_meta_data": {"title": "Other", "creator": "Someone"},
            }
        )
    )
    assert store.state.now_playing[side].content_ref is None
    assert store.state.now_playing[side].source == "pandora"
    await a.play_station("sonos-RINCON_K", ref, stations[0])
    sub.callback(
        Event(
            {
                "transport_state": "PLAYING",
                "current_track_uri": "x-sonosapi-radio:ST%3A1?sid=236&flags=8300&sn=4",
                "current_track_meta_data": {"title": "Ember", "creator": "Low Tide"},
            }
        )
    )
    np = store.state.now_playing[side]
    assert np.title == "Ember" and np.content_ref == ref and np.seekable is False
    sub.callback(
        Event(
            {
                "transport_state": "PLAYING",
                "current_track_uri": "x-sonos-http:track/77.flac?sid=174&sn=3",
                "current_track_meta_data": {"title": "Signal", "creator": "Analog Heart"},
            }
        )
    )
    np = store.state.now_playing[side]
    assert np.content_ref is not None and np.content_ref.service == "tidal"
    assert side not in a._station_playing

    # A vendor failure restores the previous now-playing and station map.
    before = store.state.now_playing[side]

    def boom(*_a, **_k):
        LazyZone._guard()
        raise OSError("player unreachable")

    k.play_uri = boom  # type: ignore[attr-defined]
    with pytest.raises(OSError):
        await a.play_station("sonos-RINCON_K", ref, stations[1])
    assert store.state.now_playing[side] == before
    assert side not in a._station_playing  # the Tidal event had already cleared it
    with pytest.raises(ContentUnavailableError):
        await a.play_station("sonos-RINCON_K", ref, stations[0].model_copy(update={"ids": {}}))
    with pytest.raises(ContentUnavailableError):
        await a.play_station(
            "sonos-RINCON_K", ContentRef(service="tidal", kind="track", id="1"), stations[0]
        )
    a.settings = settings
    with pytest.raises(ContentUnavailableError):
        await a.play_station("sonos-RINCON_K", ref, stations[0])
    await a.disconnect()


async def test_transitioning_holds_the_known_play_state(
    store: StateStore, settings: Settings
) -> None:
    """Sonos reports TRANSITIONING for ~1s while a stream buffers; that must not blank state.

    Measured on a Sonos Amp: `t+01s TRANSITIONING`, `t+02s PLAYING`. Writing "unknown" through
    would overwrite the client's optimistic play, so a tapped play button flicks back for a
    second before it takes. It also accounted for play confirming in 1.7-1.9s while pause
    confirmed in 0.21s.
    """
    h = Harness()
    a = h.adapter(store, settings)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")

    h.sub("RINCON_K", "avTransport").callback(Event({"transport_state": "PAUSED_PLAYBACK"}))
    await wait_for(lambda: store.state.players["sonos-RINCON_K"].play_state == "pause")

    h.sub("RINCON_K", "avTransport").callback(Event({"transport_state": "TRANSITIONING"}))
    assert store.state.players["sonos-RINCON_K"].play_state == "pause"  # held, not blanked

    h.sub("RINCON_K", "avTransport").callback(Event({"transport_state": "PLAYING"}))
    await wait_for(lambda: store.state.players["sonos-RINCON_K"].play_state == "play")
    await a.disconnect()


def test_play_state_mapping_returns_none_for_states_without_meaning() -> None:
    assert sonos_mod._play_state("PLAYING") == "play"
    assert sonos_mod._play_state("PAUSED_PLAYBACK") == "pause"
    assert sonos_mod._play_state("STOPPED") == "stop"
    # TRANSITIONING and anything unrecognised carry no play/pause/stop meaning.
    assert sonos_mod._play_state("TRANSITIONING") is None
    assert sonos_mod._play_state(None) is None
    assert sonos_mod._play_state("NONSENSE") is None
