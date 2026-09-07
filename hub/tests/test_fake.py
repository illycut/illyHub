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
    assert {p.name for p in s.players.values()} == {"Living Room Amp", "Den", "Kitchen", "Patio"}
    assert (
        s.zones[fake_zone_id("main")].power is True
        and s.zones[fake_zone_id("zone2")].power is False
    )
    assert s.zones[fake_zone_id("main")].player_ids == [HEOS_PLAYER]
    assert s.sides[b.sonos.side_id()].coordinator_player_id == KITCHEN
    assert s.sides["heos:heos-1"].capabilities.supports_power is True
    assert s.now_playing["heos:heos-1"].title == DEFAULT_TRACK.title
    assert all(c.state == "connected" for c in s.connections.values())
    b.heos.sim_play_state(HEOS_PLAYER, "play")
    await asyncio.sleep(0.05)
    assert store.state.positions["heos:heos-1"].position_ms >= 20
    assert store.state.sides["heos:heos-1"].play_state == "play"
    b.heos.sim_play_state(HEOS_PLAYER, "pause")
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
    b.sonos.sim_volume(PATIO, 70, muted=True)
    p = store.state.players[PATIO]
    assert p.volume == 70 and p.muted is True
    b.sonos.sim_play_state(PATIO, "play")
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


async def test_fake_groups_follow_device_semantics(store: StateStore) -> None:
    from illyhub_hub.adapters.fake import HEOS_PLAYER_2

    b = FakeBundle(store, tick_interval_s=10)
    await b.start()
    # Real id shapes: heos-g<n>, sonos-g<RINCON uid>:<n>.
    assert store.state.players[PATIO].group_id == f"sonos-g{KITCHEN.removeprefix('sonos-')}:1"
    await b.heos.set_group([HEOS_PLAYER, HEOS_PLAYER_2])
    gid = store.state.players[HEOS_PLAYER_2].group_id
    assert gid is not None and gid.startswith("heos-g") and gid[6:].isdigit()
    # A follower leaving keeps the group only if 2+ remain; a leader leaving dissolves it.
    await b.heos.set_group([HEOS_PLAYER_2])
    assert store.state.players[HEOS_PLAYER].group_id is None
    await b.heos.set_group([HEOS_PLAYER, HEOS_PLAYER_2])
    await b.heos.set_group([HEOS_PLAYER])  # leader leaves -> dissolved, Den not orphaned
    assert store.state.players[HEOS_PLAYER_2].group_id is None
    assert not [g for g in store.state.groups.values() if g.vendor == "heos"]
    # Sonos reconcile: same members -> nothing changes; drop Patio -> Kitchen solo.
    before = store.state.version
    await b.sonos.set_group([KITCHEN, PATIO])
    assert store.state.players[PATIO].group_id is not None
    await b.sonos.set_group([KITCHEN])
    assert store.state.players[PATIO].group_id is None and store.state.version > before
    # Coordinator unjoin hands the group to the next member instead of clearing everything.
    await b.sonos.set_group([KITCHEN, PATIO])
    b.sonos.store.upsert_player(
        b.sonos.store.state.players[PATIO].model_copy(update={"id": "sonos-RINCON_X", "name": "X"})
    )
    await b.sonos.set_group([KITCHEN, PATIO, "sonos-RINCON_X"])
    await b.sonos.set_group([KITCHEN])
    g = next(g for g in store.state.groups.values() if g.vendor == "sonos")
    assert set(g.member_ids) == {PATIO, "sonos-RINCON_X"} and g.coordinator_player_id == PATIO
    await b.sonos.dissolve(PATIO, g.member_ids)
    assert not [g for g in store.state.groups.values() if g.vendor == "sonos"]
    # Seeking a paused side persists; play resumes from there.
    side = "heos:heos-1"
    b.heos.sim_play_state(HEOS_PLAYER, "pause")
    b.ticker.seek(side, 42_000)
    b.heos.sim_play_state(HEOS_PLAYER, "play")
    b.ticker.tick()
    assert store.state.positions[side].position_ms > 42_000
    await b.stop()
