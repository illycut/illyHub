from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any

import pytest

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
    assert np.art.url == "http://192.168.1.31:1400/getaa?s=1&u=x"
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
