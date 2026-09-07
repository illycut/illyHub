from __future__ import annotations

import asyncio

import pytest

from illyhub_hub.adapters.fake import (
    DEFAULT_TRACK,
    HEOS_PLAYER,
    KITCHEN,
    PATIO,
    FakeBundle,
    FakeTrack,
    fake_zone_id,
)
from illyhub_hub.state import StateStore


async def test_bundle_seeds_scenario_and_ticks_positions(store: StateStore) -> None:
    b = FakeBundle(store, tick_interval_s=0.01)
    await b.start()
    s = store.state
    assert {p.name for p in s.players.values()} == {"Living Room Amp", "Kitchen", "Patio"}
    assert (
        s.zones[fake_zone_id("main")].power is True
        and s.zones[fake_zone_id("zone2")].power is False
    )
    assert s.zones[fake_zone_id("main")].player_ids == [HEOS_PLAYER]
    assert s.sides[b.sonos.side_id()].coordinator_player_id == KITCHEN
    assert s.sides["heos:heos-1"].capabilities.supports_power is True
    assert s.now_playing["heos:heos-1"].title == DEFAULT_TRACK.title
    assert all(c.state == "connected" for c in s.connections.values())
    b.heos.set_play_state(HEOS_PLAYER, "play")
    await asyncio.sleep(0.05)
    assert store.state.positions["heos:heos-1"].position_ms >= 20
    assert store.state.sides["heos:heos-1"].play_state == "play"
    b.heos.set_play_state(HEOS_PLAYER, "pause")
    frozen = store.state.positions["heos:heos-1"].position_ms
    await asyncio.sleep(0.03)
    assert store.state.positions["heos:heos-1"].position_ms == frozen
    await b.stop()
    assert all(c.state == "disconnected" for c in store.state.connections.values())


async def test_scripting_methods(store: StateStore) -> None:
    b = FakeBundle(store, tick_interval_s=0.01)
    events: list[str] = []
    for a in b.adapters:
        a.on_event(lambda e: events.append(f"{e.adapter}:{e.kind}"))
    await b.start()
    await b.start()  # idempotent connects
    b.sonos.set_volume(PATIO, 70, muted=True)
    p = store.state.players[PATIO]
    assert p.volume == 70 and p.muted is True
    b.sonos.set_play_state(PATIO, "play")
    assert store.state.players[KITCHEN].play_state == "play"  # whole side
    b.sonos.change_track(KITCHEN, FakeTrack("Station", "Pandora", "", 0, source="pandora"))
    np = store.state.now_playing[b.sonos.side_id()]
    assert np.seekable is False and np.source == "pandora"
    assert store.state.sides[b.sonos.side_id()].capabilities.supports_seek is False
    await asyncio.sleep(0.03)
    assert store.state.positions[b.sonos.side_id()].position_ms > 0
    b.sonos.ungroup_all()
    assert set(store.state.sides) >= {f"sonos:{KITCHEN}", f"sonos:{PATIO}"}
    assert b.sonos.side_id() == f"sonos:{KITCHEN}"
    b.sonos.group([KITCHEN], coordinator=KITCHEN)  # single member = no group
    assert b.sonos.group_id(KITCHEN) not in store.state.groups
    b.heos.drop()
    assert store.state.players[HEOS_PLAYER].online is False
    assert store.state.connections["heos"].state == "reconnecting"
    b.heos.restore()
    assert store.state.players[HEOS_PLAYER].online is True
    await b.denon.set_power("zone2", True)  # bare key
    await b.denon.set_power(fake_zone_id("main"), False)  # namespaced id
    assert store.state.zones[fake_zone_id("zone2")].power is True
    assert store.state.zones[fake_zone_id("main")].power is False
    with pytest.raises(ValueError):
        await b.denon.set_power("nope", True)
    assert (
        "sonos:volume" in events and "heos:disconnected" in events and "denon:zone_power" in events
    )
    await b.stop()
