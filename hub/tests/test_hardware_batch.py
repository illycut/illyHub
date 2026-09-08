"""Hardware-driven follow-ups that upstream's LAN-run commits do not already cover.

Volume routing, zone targets and the play-state transient are tested upstream
(test_commands.py, test_heos.py, test_sonos.py). Kept here: the amplifier zone as an HTTP volume
target mirroring onto the hosted player, and MVMAX read-backs never moving the volume scale.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from illyhub_hub.adapters.fake import HEOS_PLAYER
from illyhub_hub.api import HubRuntime, create_app
from illyhub_hub.config import Settings
from illyhub_hub.state import StateStore, zone_for_player
from test_denon import MAIN, FakeReceiver, HttpRecorder, make, settle

HDR = {"X-Illyhub": "1"}


async def test_zone_volume_over_the_api_mirrors_onto_the_hosted_player(tmp_path: Path) -> None:
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
            zone = zone_for_player(rt.store.state, HEOS_PLAYER)
            assert zone is not None
            r = await c.post("/api/volume", json={"target": zone.id, "level": 33}, headers=HDR)
            assert r.status_code == 200, r.text
            assert r.json()["applied"] == [zone.id]
            assert rt.store.state.zones[zone.id].volume == 33
            # The AVR-hosted player shows the zone's (quantised) level, never HEOS's 0.
            player = rt.store.state.players[HEOS_PLAYER]
            assert player.volume == rt.store.state.zones[zone.id].volume
            r = await c.post("/api/volume", json={"target": HEOS_PLAYER, "level": 44}, headers=HDR)
            assert r.status_code == 200
            dev = (await c.get("/api/devices")).json()
            shown = next(p for p in dev["players"] if p["id"] == HEOS_PLAYER)
            assert shown["volume"] == rt.store.state.zones[zone.id].volume


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
