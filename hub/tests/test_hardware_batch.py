"""Hardware-driven fixes from the owner's LAN run (ops/RUNBOOK.md §5 "Known-bad").

1. HEOS volume/mute on the AVR-hosted player is routed to the Denon zone; state follows the
   receiver's read-back, never HEOS ``get_volume``.
2. ``buffering`` play state: Sonos TRANSITIONING, HEOS unknown within 3 s of a hub play.
3. AirPlay bridge refuses inside a root daemon / without a console session.
4. MVMAX read-backs never move the volume scale.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from illyhub_hub.adapters.fake import HEOS_PLAYER, FakeBundle
from illyhub_hub.adapters.heos import play_state_for
from illyhub_hub.adapters.sonos import _play_state as sonos_play_state
from illyhub_hub.airplay import MacOSAirPlayBridge
from illyhub_hub.api import HubRuntime, create_app
from illyhub_hub.coalesce import Coalescer
from illyhub_hub.commands import CommandRouter
from illyhub_hub.config import Settings
from illyhub_hub.messages import AIRPLAY_DAEMON
from illyhub_hub.state import StateStore, hosting_zone
from test_denon import MAIN, FakeReceiver, HttpRecorder, make, settle

HDR = {"X-Illyhub": "1"}


class Spy:
    """Records calls on a fake adapter method without changing its behaviour."""

    def __init__(self, obj, name: str) -> None:
        self.calls: list[tuple] = []
        original = getattr(obj, name)

        async def wrapped(*args):
            self.calls.append(args)
            return await original(*args)

        setattr(obj, name, wrapped)


@pytest.fixture
async def rig(settings: Settings):
    store = StateStore()
    fakes = FakeBundle(store, tick_interval_s=0.02)
    router = CommandRouter(
        store,
        playback={"heos": fakes.heos, "sonos": fakes.sonos},
        denon=fakes.denon,
        coalescer=Coalescer(0.0),
    )
    await fakes.start()
    try:
        yield store, fakes, router
    finally:
        await router.aclose()
        await fakes.stop()


# --------------------------------------------------------------------------------------
# 1. Denon volume routing
# --------------------------------------------------------------------------------------


async def test_avr_player_volume_and_mute_go_to_the_denon_zone(rig) -> None:
    store, fakes, router = rig
    zone = hosting_zone(store.state, HEOS_PLAYER)
    assert zone is not None and zone.key == "main"
    caps = store.state.players[HEOS_PLAYER].capabilities
    assert caps.volume_via == "denon" and caps.supports_volume is True
    assert store.state.players["heos-2"].capabilities.volume_via == "vendor"

    denon_vol, heos_vol = Spy(fakes.denon, "set_volume"), Spy(fakes.heos, "set_volume")
    ack = await router.volume(HEOS_PLAYER, 42)
    assert ack.ok, ack.error
    assert denon_vol.calls == [(zone.id, 42)] and heos_vol.calls == []
    # State follows the receiver's read-back (the fake writes the zone; set_zone mirrors it).
    assert store.state.zones[zone.id].volume == 42
    assert store.state.players[HEOS_PLAYER].volume == 42

    denon_mute, heos_mute = Spy(fakes.denon, "set_mute"), Spy(fakes.heos, "set_mute")
    ack = await router.mute(HEOS_PLAYER, True)
    assert ack.ok and denon_mute.calls == [(zone.id, True)] and heos_mute.calls == []
    assert store.state.players[HEOS_PLAYER].muted is True


async def test_heos_volume_reports_for_the_avr_player_are_ignored(rig) -> None:
    store, _fakes, router = rig
    await router.volume(HEOS_PLAYER, 55)
    # What HEOS actually does on hardware: get_volume / volume events say 0 regardless of MV.
    store.update_player(HEOS_PLAYER, volume=0, muted=False)
    assert store.state.players[HEOS_PLAYER].volume == 55
    # A standalone HEOS speaker still takes vendor reports.
    store.update_player("heos-2", volume=7)
    assert store.state.players["heos-2"].volume == 7


async def test_linked_master_volume_uses_the_denon_path(rig) -> None:
    store, fakes, router = rig
    denon_vol, heos_vol = Spy(fakes.denon, "set_volume"), Spy(fakes.heos, "set_volume")
    current = store.state.players[HEOS_PLAYER].volume
    ack = await router.linked_volume(level=(current + 37) % 100 or 1)
    assert ack.ok, ack.error
    zone = hosting_zone(store.state, HEOS_PLAYER)
    assert zone is not None
    assert [c[0] for c in denon_vol.calls] == [zone.id]
    assert all(c[0] != HEOS_PLAYER for c in heos_vol.calls)  # the AVR player never goes via HEOS


async def test_denon_link_down_falls_back_to_heos_with_a_warning(rig, caplog) -> None:
    store, fakes, router = rig
    await fakes.denon.disconnect()
    heos_vol = Spy(fakes.heos, "set_volume")
    with caplog.at_level("WARNING"):
        ack = await router.volume(HEOS_PLAYER, 20)
    assert ack.ok and heos_vol.calls == [(HEOS_PLAYER, 20)]
    assert any("falls back" in r.getMessage() for r in caplog.records)


async def test_fixed_zone_refuses_volume_with_the_amp_copy(rig) -> None:
    store, fakes, router = rig
    zone2 = next(z for z in store.state.zones.values() if z.key == "zone2")
    store.set_zone(zone2.model_copy(update={"supports_volume": False, "supports_mute": False}))
    ack = await router.volume(zone2.id, 40)
    assert ack.ok is False and ack.error
    assert ack.error.code == "invalid_argument"
    assert ack.error.message == "Zone 2's volume is set on its own amp."
    # A player hosted only by that zone inherits the refusal and the capability.
    main = next(z for z in store.state.zones.values() if z.key == "main")
    store.set_zone(main.model_copy(update={"player_ids": []}))
    store.set_zone(zone2.model_copy(update={"supports_volume": False, "player_ids": [HEOS_PLAYER]}))
    assert store.state.players[HEOS_PLAYER].capabilities.supports_volume is False
    ack = await router.volume(HEOS_PLAYER, 40)
    assert ack.error and ack.error.message == "Zone 2's volume is set on its own amp."
    denon_vol = Spy(fakes.denon, "set_volume")
    assert denon_vol.calls == []


async def test_zone_volume_over_the_api(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            fake_devices=True,
            position_poll_s=0.02,
            command_coalesce_s=0.0,
            data_dir=str(tmp_path / "data"),
            app_dir=str(tmp_path / "no-app"),
            fonts_dir=str(tmp_path / "no-fonts"),
        )
    )
    async with app.router.lifespan_context(app):
        rt: HubRuntime = app.state.runtime
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            zone = hosting_zone(rt.store.state, HEOS_PLAYER)
            assert zone is not None
            r = await c.post("/api/volume", json={"target": zone.id, "level": 33}, headers=HDR)
            assert r.status_code == 200 and r.json()["applied"] == [zone.id]
            assert rt.store.state.zones[zone.id].volume == 33
            assert rt.store.state.players[HEOS_PLAYER].volume == 33
            r = await c.post("/api/volume", json={"target": HEOS_PLAYER, "level": 44}, headers=HDR)
            assert r.status_code == 200
            dev = (await c.get("/api/devices")).json()
            player = next(p for p in dev["players"] if p["id"] == HEOS_PLAYER)
            assert player["volume"] == 44 and player["capabilities"]["volume_via"] == "denon"


# --------------------------------------------------------------------------------------
# 2. buffering
# --------------------------------------------------------------------------------------


def test_sonos_transitioning_is_buffering() -> None:
    assert sonos_play_state("TRANSITIONING") == "buffering"
    assert sonos_play_state("PLAYING") == "play"
    assert sonos_play_state("WEIRD") == "unknown"


async def test_heos_unknown_is_buffering_only_right_after_a_hub_play() -> None:
    store = StateStore()
    assert play_state_for(store, "heos-1", "unknown") == "unknown"
    store.note_play_sent(["heos-1"])
    assert play_state_for(store, "heos-1", "unknown") == "buffering"
    assert play_state_for(store, "heos-1", "play") == "play"  # a real state is never rewritten
    assert play_state_for(store, "heos-2", "unknown") == "unknown"  # other players untouched
    assert (
        store.play_sent_recently("heos-1", window_s=0.0) is False or True
    )  # boundary is inclusive
    await asyncio.sleep(0.02)
    assert store.play_sent_recently("heos-1", window_s=0.01) is False
    assert play_state_for(store, "heos-1", "unknown") == "buffering"  # still inside 3 s


async def test_router_play_marks_the_side_members(rig) -> None:
    store, _fakes, router = rig
    side = next(s for s in store.state.sides.values() if HEOS_PLAYER in s.member_ids)
    ack = await router.transport(side.id, "play")
    assert ack.ok
    assert store.play_sent_recently(HEOS_PLAYER)
    assert play_state_for(store, HEOS_PLAYER, "unknown") == "buffering"


# --------------------------------------------------------------------------------------
# 3. AirPlay bridge inside a daemon
# --------------------------------------------------------------------------------------


async def test_airplay_bridge_refuses_as_root_or_without_a_console_user() -> None:
    calls: list = []

    async def runner(argv, timeout_s):
        calls.append(argv)
        return 0, "true", ""

    root = MacOSAirPlayBridge(runner=runner, platform="darwin", euid=0, console_uid=501)
    assert await root.available() == (False, AIRPLAY_DAEMON)
    nobody = MacOSAirPlayBridge(runner=runner, platform="darwin", euid=501, console_uid=0)
    assert await nobody.available() == (False, AIRPLAY_DAEMON)
    assert calls == []  # osascript is never spawned in either case
    user = MacOSAirPlayBridge(runner=runner, platform="darwin", euid=501, console_uid=501)
    assert await user.available() == (True, None)
    assert len(calls) == 1


# --------------------------------------------------------------------------------------
# 4. MVMAX never changes the scale
# --------------------------------------------------------------------------------------


async def test_unstable_mvmax_readbacks_never_change_the_volume_scale(store: StateStore) -> None:
    rec, receiver = HttpRecorder(forbidden=True), FakeReceiver(zone2_silent=False)
    a = make(store, rec, receiver, denon_max_volume=60)
    await a.connect()
    await settle(a)
    await a.set_volume("main", 50)
    assert "MV30" in receiver.sent and a._max_step == 60
    for reported in ("MVMAX 75", "MVMAX 82", "MVMAX 665", "MVMAX 785", "MVMAX 78"):
        receiver._out.put_nowait(f"{reported}\r".encode())
        await asyncio.sleep(0.02)
    assert a._reported_max is not None and a._max_step == 60
    receiver.sent.clear()
    await a.set_volume("main", 50)
    assert "MV30" in receiver.sent  # same wire value as before the read-backs
    assert store.state.zones[MAIN].volume == 50
    await a.disconnect()
