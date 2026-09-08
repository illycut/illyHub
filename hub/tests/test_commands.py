from __future__ import annotations

import asyncio

import pytest

from illyhub_hub.adapters.base import UnsupportedCommandError
from illyhub_hub.adapters.fake import HEOS_PLAYER, KITCHEN, PATIO, FakeBundle
from illyhub_hub.coalesce import Coalescer
from illyhub_hub.commands import (
    ERROR_CATALOGUE,
    Ack,
    CommandError,
    CommandRouter,
    catalogue,
    http_status,
    linked_volumes,
    resolve_players,
    resolve_sides,
)
from illyhub_hub.logsetup import correlation_id
from illyhub_hub.state import StateStore

HEOS_SIDE = "heos:heos-1"


@pytest.fixture
async def rig(store: StateStore):
    b = FakeBundle(store, tick_interval_s=0.01)
    await b.start()
    router = CommandRouter(
        store,
        playback={"heos": b.heos, "sonos": b.sonos},
        denon=b.denon,
        coalescer=Coalescer(0.0),
    )
    acks: list[Ack] = []
    router.on_ack(acks.append)
    yield b, router, acks
    await router.aclose()
    await b.stop()


def test_resolvers_and_catalogue(store: StateStore) -> None:
    with pytest.raises(CommandError) as ei:
        resolve_sides(store.state, "nope")
    assert ei.value.code == "unknown_target" and ei.value.target == "nope"
    with pytest.raises(CommandError):
        resolve_players(store.state, "nope")
    assert http_status("unknown_target") == 404 and http_status("made_up") == 500
    assert {c for c, _, _ in catalogue()} == set(ERROR_CATALOGUE)


def test_linked_volume_math() -> None:
    lv = linked_volumes
    assert lv({"a": 40, "b": 20}, {}, level=80, delta=None)[0] == {"a": 80, "b": 40}
    assert lv({"a": 40, "b": 20}, {}, level=None, delta=-20)[0] == {"a": 20, "b": 10}
    assert lv({"a": 0, "b": 0}, {}, level=None, delta=10)[0] == {"a": 10, "b": 10}
    assert lv({"a": 90, "b": 45}, {}, level=None, delta=+30)[0] == {"a": 100, "b": 50}
    assert lv({}, {}, level=5, delta=None) == ({}, {})
    with pytest.raises(CommandError):
        lv({"a": 1}, {}, level=None, delta=None)


def test_linked_volume_round_trip_keeps_quiet_players_alive() -> None:
    """100/1/50 -> -50 -> +50 must come back to 100/1/50, not 100/0/50."""
    current = {"a": 100, "b": 1, "c": 50}
    down, ratios = linked_volumes(current, {}, level=None, delta=-50)
    assert down == {"a": 50, "b": 1, "c": 25}
    up, ratios = linked_volumes(down, ratios, level=None, delta=+50)
    assert up == {"a": 100, "b": 1, "c": 50}
    # Even when the float ratio rounds to 0, a non-zero player is floor-clamped to 1 ...
    tiny, ratios = linked_volumes({"a": 100, "b": 1}, {}, level=20, delta=None)
    assert tiny == {"a": 20, "b": 1}
    # ... and the remembered ratio (not 1/20) drives the way back up.
    back, _ = linked_volumes(tiny, ratios, level=100, delta=None)
    assert back == {"a": 100, "b": 1}


async def test_transport_resolves_player_side_and_all(rig) -> None:
    b, router, acks = rig
    token = correlation_id.set("cid-1")
    try:
        ack = await router.transport(HEOS_PLAYER, "play")
    finally:
        correlation_id.reset(token)
    assert (
        ack.ok and ack.correlation_id == "cid-1" and ack.state_version == b.heos.store.state.version
    )
    assert b.heos.store.state.players[HEOS_PLAYER].play_state == "play"
    ack = await router.transport(HEOS_SIDE, "toggle")
    assert ack.ok and b.heos.store.state.players[HEOS_PLAYER].play_state == "pause"
    ack = await router.transport("all", "play")
    assert ack.ok
    assert all(p.play_state == "play" for p in b.heos.store.state.players.values())
    assert await router.transport(PATIO, "next")  # member id resolves to its side's coordinator
    assert (await router.transport("all", "stop")).ok
    assert (await router.transport("all", "prev")).ok
    assert acks and all(a.ok for a in acks)


async def test_unknown_target_and_disconnected_adapter(rig) -> None:
    b, router, acks = rig
    ack = await router.transport("ghost", "play")
    assert not ack.ok and ack.error is not None and ack.error.code == "unknown_target"
    assert ack.error.message == "Nothing is called 'ghost'."
    b.heos.drop()
    ack = await router.transport(HEOS_PLAYER, "play")
    assert ack.error is not None and ack.error.code == "adapter_disconnected"
    assert "HEOS link is down" in ack.error.message
    b.heos.restore()
    b.heos.store.update_player(HEOS_PLAYER, online=False)
    ack = await router.transport(HEOS_PLAYER, "play")
    assert ack.error is not None and ack.error.code == "device_offline"


async def test_seek_and_skip_on_sonos_and_refused_on_heos(rig) -> None:
    b, router, _ = rig
    side = b.sonos.side_id()
    b.sonos.change_track(KITCHEN)
    await router.transport(KITCHEN, "play")
    ack = await router.seek(KITCHEN, 30_000)
    assert ack.ok and b.sonos.store.state.positions[side].position_ms == 30_000
    ack = await router.skip(KITCHEN, 15_000)
    assert ack.ok and b.sonos.store.state.positions[side].position_ms == 45_000
    ack = await router.skip(KITCHEN, -60_000)  # clamps at 0
    assert ack.ok and b.sonos.store.state.positions[side].position_ms == 0
    ack = await router.seek(KITCHEN, 10_000_000)  # clamps at duration
    assert ack.ok and b.sonos.store.state.positions[side].position_ms == 213_000
    ack = await router.seek(KITCHEN, -1)  # validation failures are acks too, never raises
    assert ack.error is not None and ack.error.code == "invalid_argument"
    # HEOS cannot seek at all (CLI limitation); capability says so and the error is specific.
    ack = await router.seek(HEOS_PLAYER, 1000)
    assert ack.error is not None and ack.error.code == "unsupported_action"
    ack = await router.skip(HEOS_PLAYER)
    assert ack.error is not None and ack.error.code == "unsupported_action"


async def test_seek_refused_for_radio_source(rig) -> None:
    from illyhub_hub.adapters.fake import FakeTrack

    b, router, _ = rig
    b.sonos.change_track(KITCHEN, FakeTrack("Station", "Pandora", "", 0, source="pandora"))
    ack = await router.seek(KITCHEN, 5000)
    assert ack.error is not None and ack.error.code == "not_seekable"
    assert "station" in ack.error.message


async def test_volume_mute_and_linked_master(rig) -> None:
    b, router, _ = rig
    store = b.heos.store
    assert (await router.volume(PATIO, 55)).ok and store.state.players[PATIO].volume == 55
    ack = await router.volume(b.sonos.side_id(), 30)  # side -> every member
    assert ack.ok and store.state.players[KITCHEN].volume == 30
    assert store.state.players[PATIO].volume == 30
    ack = await router.volume(PATIO, 101)
    assert ack.error is not None and ack.error.code == "invalid_argument"
    assert (await router.mute(HEOS_PLAYER, True)).ok
    assert store.state.players[HEOS_PLAYER].muted is True
    # Linked: heos 35, kitchen 30, patio 30 -> master (max) to 70 doubles everyone.
    await router.volume(HEOS_PLAYER, 35)
    ack = await router.linked_volume(level=70)
    assert ack.ok and ack.action == "linked_volume"
    assert store.state.players[HEOS_PLAYER].volume == 70
    assert store.state.players[KITCHEN].volume == 60
    ack = await router.linked_volume(delta=-35)
    assert store.state.players[HEOS_PLAYER].volume == 35
    assert store.state.players[KITCHEN].volume == 30
    b.heos.store.mark_vendor_offline("heos")
    b.heos.store.mark_vendor_offline("sonos")
    ack = await router.linked_volume(delta=5)
    assert ack.error is not None and ack.error.code == "device_offline"


async def test_volume_burst_is_coalesced(store: StateStore) -> None:
    b = FakeBundle(store, tick_interval_s=0.01)
    await b.start()
    coalescer = Coalescer(0.1)
    router = CommandRouter(store, playback={"heos": b.heos, "sonos": b.sonos}, coalescer=coalescer)
    for level in range(10, 60):
        await router.volume(PATIO, level)
    assert coalescer.writes == 1 and store.state.players[PATIO].volume == 10
    await asyncio.sleep(0.15)
    assert coalescer.writes == 2 and store.state.players[PATIO].volume == 59
    await router.aclose()
    await b.stop()


async def test_zone_power_denon_and_sonos_off_semantics(rig) -> None:
    b, router, _ = rig
    store = b.heos.store
    zid = "denon-fake:zone2"
    assert (await router.zone_power(zid, True)).ok and store.state.zones[zid].power is True
    assert (await router.zone_power(zid, False)).ok and store.state.zones[zid].power is False
    store.set_zone(store.state.zones[zid].model_copy(update={"online": False}))
    ack = await router.zone_power(zid, True)
    assert ack.error is not None and ack.error.code == "device_offline"
    router.denon = None
    ack = await router.zone_power("denon-fake:main", True)
    assert ack.error is not None and ack.error.code == "adapter_disconnected"
    # Sonos "off": stop + ungroup; "on": no-op.
    side = b.sonos.side_id()
    await router.transport(side, "play")
    ack = await router.zone_power(side, False)
    assert ack.ok
    assert store.state.players[KITCHEN].play_state == "stop"
    assert store.state.players[KITCHEN].group_id is None
    assert (await router.zone_power(KITCHEN, True)).ok
    ack = await router.zone_power(HEOS_PLAYER, False)
    assert ack.error is not None and ack.error.code == "unsupported_action"
    ack = await router.zone_power("ghost", False)
    assert ack.error is not None and ack.error.code == "unknown_target"


async def test_group_and_ungroup(rig) -> None:
    b, router, _ = rig
    store = b.heos.store
    side = b.sonos.side_id()
    assert (await router.ungroup(side)).ok
    assert store.state.players[PATIO].group_id is None and len(store.state.sides) == 4
    ack = await router.group("sonos", PATIO, [KITCHEN, PATIO])
    assert ack.ok and store.state.players[KITCHEN].group_id == b.sonos.group_id(PATIO)
    assert store.state.sides[b.sonos.side_id(PATIO)].coordinator_player_id == PATIO
    ack = await router.group("sonos", PATIO, [HEOS_PLAYER])
    assert ack.error is not None and ack.error.code == "invalid_argument"
    ack = await router.group("sonos", PATIO, ["ghost"])
    assert ack.error is not None and ack.error.code == "unknown_target"
    caps = store.state.players[KITCHEN].capabilities.model_copy(update={"can_group": False})
    store.update_player(KITCHEN, capabilities=caps)
    ack = await router.group("sonos", PATIO, [KITCHEN])
    assert ack.error is not None and "can't be grouped" in ack.error.message
    ack = await router.ungroup("ghost")
    assert ack.error is not None and ack.error.code == "unknown_target"
    assert (await router.ungroup(HEOS_SIDE)).ok  # solo side: dissolve is a no-op group call
    # HEOS dissolve goes through the *leader* (fake mirrors group/set_group semantics).
    from illyhub_hub.adapters.fake import HEOS_PLAYER_2

    assert (await router.group("heos", HEOS_PLAYER, [HEOS_PLAYER_2])).ok
    gid = store.state.players[HEOS_PLAYER_2].group_id
    assert gid is not None
    assert (await router.ungroup(f"heos:{gid}")).ok
    assert store.state.players[HEOS_PLAYER_2].group_id is None
    assert store.state.players[HEOS_PLAYER].group_id is None


async def test_vendor_error_and_unsupported_are_mapped(rig) -> None:
    b, router, _ = rig

    async def boom(_pid: str) -> None:
        raise RuntimeError("UPnP 500")

    async def nope(_pid: str) -> None:
        raise UnsupportedCommandError("no")

    b.sonos.play = boom  # type: ignore[method-assign]
    ack = await router.transport(KITCHEN, "play")
    assert ack.error is not None and ack.error.code == "vendor_error"
    assert ack.error.message == "Play didn't happen on Kitchen + Patio."
    b.sonos.next = nope  # type: ignore[method-assign]
    ack = await router.transport(KITCHEN, "next")
    assert ack.error is not None and ack.error.code == "unsupported_action"


async def test_ack_callbacks_async_and_failing(rig) -> None:
    b, router, acks = rig
    seen: list[str] = []

    async def async_cb(ack: Ack) -> None:
        seen.append(ack.action)

    def bad_cb(_ack: Ack) -> None:
        raise RuntimeError("listener bug")

    unsub = router.on_ack(async_cb)
    router.on_ack(bad_cb)
    await router.transport(HEOS_PLAYER, "play")
    await asyncio.sleep(0)
    assert seen == ["play"]
    unsub()
    unsub()
    await router.transport(HEOS_PLAYER, "pause")
    await asyncio.sleep(0)
    assert seen == ["play"] and len(acks) == 2


@pytest.fixture
async def slow_rig(store: StateStore):
    """Router at the production coalesce interval (0.1 s)."""
    b = FakeBundle(store, tick_interval_s=10)
    await b.start()
    router = CommandRouter(
        store,
        playback={"heos": b.heos, "sonos": b.sonos},
        denon=b.denon,
        coalescer=Coalescer(0.1),
    )
    yield b, router
    await router.aclose()
    await b.stop()


async def test_rapid_skips_accumulate_into_one_absolute_seek(slow_rig) -> None:
    b, router = slow_rig
    side = b.sonos.side_id()
    b.sonos.change_track(KITCHEN)
    await router.transport(KITCHEN, "pause")  # no extrapolation while paused
    acks = [await router.skip(KITCHEN) for _ in range(3)]
    assert all(a.ok for a in acks)
    assert router.coalescer.writes == 1  # first skip wrote 15 s immediately
    await asyncio.sleep(0.15)
    assert router.coalescer.writes == 2  # the other two collapsed into one 45 s seek
    assert b.sonos.store.state.positions[side].position_ms == 45_000
    assert not router._pending_seek


async def test_skip_extrapolates_from_reported_at_while_playing(store: StateStore) -> None:
    from datetime import UTC, datetime, timedelta

    b = FakeBundle(store, tick_interval_s=10)
    await b.start()
    t0 = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    router = CommandRouter(
        store,
        playback={"heos": b.heos, "sonos": b.sonos},
        coalescer=Coalescer(0.0),
        clock=lambda: t0 + timedelta(seconds=4),
    )
    b.sonos.change_track(KITCHEN)
    await router.transport(KITCHEN, "play")
    side = b.sonos.side_id()
    from illyhub_hub.state import Position

    store.set_position(side, Position(position_ms=10_000, reported_at=t0, confidence=0.9))
    await router.skip(KITCHEN, 15_000)
    assert store.state.positions[side].position_ms == 29_000  # 10 s + 4 s elapsed + 15 s
    await router.aclose()
    await b.stop()


async def test_all_target_partial_success(rig) -> None:
    b, router, _ = rig
    store = b.heos.store
    store.mark_vendor_offline("heos")
    ack = await router.transport("all", "play")
    assert ack.ok and ack.error is None
    assert {p.target for p in ack.partial} == {"heos:heos-1", "heos:heos-2"}
    assert all(p.code == "device_offline" for p in ack.partial)
    assert store.state.players[KITCHEN].play_state == "play"
    ack = await router.mute("all", True)
    assert ack.ok and {p.target for p in ack.partial} == {HEOS_PLAYER, "heos-2"}
    ack = await router.linked_volume(delta=5)
    assert ack.ok and not ack.partial  # offline players are not attempted, so no failures
    store.mark_vendor_offline("sonos")
    ack = await router.transport("all", "pause")
    assert not ack.ok and ack.error is not None and len(ack.partial) == 3  # 3 sides, all failed
    ack = await router.transport(HEOS_PLAYER, "play")
    assert not ack.ok and ack.partial == []  # single failed target: error only, no partial


async def test_unexpected_exception_still_yields_one_ack(rig) -> None:
    b, router, acks = rig

    def boom(*_a, **_k):
        raise RuntimeError("bug in resolver")

    router._transport_side = boom  # type: ignore[method-assign]
    ack = await router.transport(HEOS_PLAYER, "play")
    assert not ack.ok and ack.error is not None and ack.error.code == "vendor_error"
    assert ack.error.message == "Play failed unexpectedly."
    assert len([a for a in acks if a.correlation_id == ack.correlation_id]) == 1


async def test_next_prev_refused_when_source_has_none(rig) -> None:
    from illyhub_hub.adapters.fake import FakeTrack

    b, router, _ = rig
    b.sonos.change_track(KITCHEN, FakeTrack("Station", "Pandora", "", 0, source="pandora"))
    ack = await router.transport(KITCHEN, "prev")
    assert ack.error is not None and ack.error.code == "unsupported_action"
    assert (await router.transport(KITCHEN, "next")).ok
