"""Phase 7 API: AirPlay bridge status/outputs, Pandora Sync start/stop, WS state, settings,
header/origin policy, mutual exclusion with Sync Play, real mode without the flag."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from illyhub_hub.airplay import FakeAirPlayBridge
from illyhub_hub.api import HubRuntime, create_app
from illyhub_hub.config import Settings
from illyhub_hub.messages import (
    AIRPLAY_NO_MATCH,
    AIRPLAY_NOTHING_TO_RESTORE,
    AIRPLAY_OFF,
    PANDORA_SYNC_BLOCKED_BY_SYNC,
    PANDORA_SYNC_STARTED,
    PANDORA_SYNC_STOPPED,
    SYNC_BLOCKED_BY_PANDORA,
)

HDR = {"X-Illyhub": "1"}
HEOS_SIDE = "heos:heos-1"
SONOS_SIDE = "sonos:sonos-gRINCON_KITCHEN:1"


def fake_settings(tmp_path: Path, **kw) -> Settings:
    return Settings(
        _env_file=None,
        fake_devices=True,
        position_poll_s=0.02,
        command_coalesce_s=0.0,
        data_dir=str(tmp_path / "data"),
        app_dir=str(tmp_path / "no-app"),
        fonts_dir=str(tmp_path / "no-fonts"),
        **kw,
    )


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, HubRuntime]]:
    app = create_app(fake_settings(tmp_path, fake_tidal=True))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c, app.state.runtime


def bridge(rt: HubRuntime) -> FakeAirPlayBridge:
    assert rt.airplay is not None and isinstance(rt.airplay.bridge, FakeAirPlayBridge)
    return rt.airplay.bridge


async def start(c: httpx.AsyncClient, **body) -> httpx.Response:
    return await c.post("/api/pandora-sync/start", json=body, headers=HDR)


async def test_airplay_status_lists_fake_outputs(client) -> None:
    c, _rt = client
    r = await c.get("/api/airplay")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and body["available"] is True and body["reason"] is None
    assert [o["name"] for o in body["outputs"]] == ["Hub Mac", "Living Room Amp", "Kitchen + Patio"]
    assert body["outputs"][0]["kind"] == "computer" and body["outputs"][0]["selected"] is True
    assert [o["kind_label"] for o in body["outputs"]] == [
        "This Mac",
        "AirPlay speaker",
        "AirPlay speaker",
    ]


async def test_airplay_outputs_selects_and_validates(client) -> None:
    c, rt = client
    r = await c.post("/api/airplay/outputs", json={"ids": ["ap-living"]}, headers=HDR)
    assert r.status_code == 200
    ack = r.json()
    assert ack["ok"] is True and ack["action"] == "airplay_outputs" and ack["target"] == "airplay"
    assert ack["applied"] == ["ap-living"]
    assert [(o.id, o.selected) for o in bridge(rt).outputs] == [
        ("mac", False),
        ("ap-living", True),
        ("ap-kitchen", False),
    ]
    assert (await c.post("/api/airplay/outputs", json={"ids": []}, headers=HDR)).status_code == 422
    too_many = {"ids": [str(i) for i in range(33)]}
    assert (await c.post("/api/airplay/outputs", json=too_many, headers=HDR)).status_code == 422
    r = await c.post("/api/airplay/outputs", json={"ids": ["ap-living", "nope"]}, headers=HDR)
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_target"


async def test_pandora_sync_start_by_sides_streams_state(client) -> None:
    c, rt = client
    seen: list[list[str]] = []
    _snapshot, unsubscribe = rt.store.subscribe(lambda _state, changed: seen.append(changed))
    try:
        r = await start(c, side_ids=[HEOS_SIDE, SONOS_SIDE])
        assert r.status_code == 200, r.text
        ack = r.json()
        assert ack["ok"] is True and ack["action"] == "pandora_sync_start"
        assert sorted(ack["applied"]) == ["ap-kitchen", "ap-living"]
        state = (await c.get("/api/pandora-sync")).json()
        assert state["active"] is True
        assert state["side_ids"] == [HEOS_SIDE, SONOS_SIDE]
        assert sorted(state["outputs"]) == ["Kitchen + Patio", "Living Room Amp"]
        assert state["previous_output_ids"] == ["mac"]
        assert state["note"] == PANDORA_SYNC_STARTED and state["started_at"]
        assert bridge(rt).pandora_opens == 1
        await asyncio.sleep(0)
        assert any("pandora_sync" in changed for changed in seen)
        assert all(p == "pandora_sync" for changed in seen for p in changed if "pandora" in p)
    finally:
        unsubscribe()


async def test_pandora_sync_start_scopes_matching_to_the_named_sides(client) -> None:
    c, rt = client
    # Only the Sonos side: the HEOS output must stay untouched.
    r = await start(c, side_ids=[SONOS_SIDE])
    assert r.status_code == 200 and r.json()["applied"] == ["ap-kitchen"]
    assert (await c.get("/api/pandora-sync")).json()["side_ids"] == [SONOS_SIDE]
    # A room with no matching output refuses and names the room.
    den = next(s for s in rt.store.state.sides.values() if s.name == "Den")
    r = await start(c, side_ids=[den.id])
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "invalid_argument"
    assert err["message"] == AIRPLAY_NO_MATCH.format(rooms="Den")
    r = await start(c, side_ids=["room:nope"])
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_target"


async def test_pandora_sync_body_needs_exactly_one_selector(client) -> None:
    c, _rt = client
    assert (await start(c)).status_code == 422
    assert (await start(c, side_ids=[])).status_code == 422
    assert (await start(c, output_ids=[])).status_code == 422
    r = await start(c, side_ids=[HEOS_SIDE], output_ids=["ap-living"])
    assert r.status_code == 422


async def test_pandora_sync_stop_restores_previous_outputs(client) -> None:
    c, rt = client
    await start(c, output_ids=["ap-living"])
    assert [o.id for o in bridge(rt).outputs if o.selected] == ["ap-living"]
    r = await c.post("/api/pandora-sync/stop", headers=HDR)
    assert r.status_code == 200
    ack = r.json()
    assert ack["ok"] is True and ack["applied"] == ["mac"]
    assert [o.id for o in bridge(rt).outputs if o.selected] == ["mac"]
    state = (await c.get("/api/pandora-sync")).json()
    assert state["active"] is False and state["note"] == PANDORA_SYNC_STOPPED
    assert state["side_ids"] == []
    r = await c.post("/api/pandora-sync/stop", headers=HDR)
    assert r.status_code == 409 and r.json()["error"]["code"] == "sync_idle"


async def test_pandora_sync_stop_with_nothing_to_restore_says_so(client) -> None:
    c, rt = client
    for o in bridge(rt).outputs:
        o.selected = False
    await start(c, output_ids=["ap-living"])
    r = await c.post("/api/pandora-sync/stop", headers=HDR)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["applied"] == []
    assert (await c.get("/api/pandora-sync")).json()["note"] == AIRPLAY_NOTHING_TO_RESTORE


async def test_pandora_sync_start_twice_keeps_the_original_restore_point(client) -> None:
    c, _rt = client
    await start(c, output_ids=["ap-living"])
    await start(c, output_ids=["ap-kitchen"])
    state = (await c.get("/api/pandora-sync")).json()
    assert state["output_ids"] == ["ap-kitchen"] and state["previous_output_ids"] == ["mac"]


async def test_pandora_sync_start_errors(client) -> None:
    c, rt = client
    r = await start(c, output_ids=["nope"])
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_target"
    b = bridge(rt)
    b.unavailable_reason = "Music isn't available on the hub Mac right now."
    r = await start(c, output_ids=["ap-living"])
    assert r.status_code == 503 and r.json()["error"]["code"] == "bridge_unavailable"
    assert r.json()["error"]["message"] == b.unavailable_reason  # full sentence, no wrapper
    status = (await c.get("/api/airplay")).json()
    assert status["available"] is False and "Music isn't available" in status["reason"]
    assert status["outputs"] == []


async def test_pandora_sync_and_sync_play_exclude_each_other(client) -> None:
    c, rt = client
    home = (await c.get("/api/home")).json()
    album = home["favorite_albums"]["items"][0]["content_ref"]
    # Sync Play live → Pandora Sync refuses.
    r = await c.post(
        "/api/sync/play",
        json={"content_ref": album, "heos_target": HEOS_SIDE, "sonos_target": SONOS_SIDE},
        headers=HDR,
    )
    assert r.status_code == 200, r.text
    r = await start(c, side_ids=[HEOS_SIDE, SONOS_SIDE])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "sync_active"
    assert r.json()["error"]["message"] == PANDORA_SYNC_BLOCKED_BY_SYNC
    assert (await c.post("/api/sync/stop", headers=HDR)).status_code == 200
    # Pandora Sync on → Sync Play refuses.
    assert (await start(c, side_ids=[HEOS_SIDE, SONOS_SIDE])).status_code == 200
    r = await c.post(
        "/api/sync/play",
        json={"content_ref": album, "heos_target": HEOS_SIDE, "sonos_target": SONOS_SIDE},
        headers=HDR,
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "bridge_active"
    assert r.json()["error"]["message"] == SYNC_BLOCKED_BY_PANDORA
    assert rt.store.state.sync.status in ("idle", "stopped")


async def test_phase7_posts_require_the_header_and_same_origin(client) -> None:
    c, _rt = client
    for path, body in (
        ("/api/pandora-sync/start", {"output_ids": ["ap-living"]}),
        ("/api/pandora-sync/stop", None),
        ("/api/airplay/outputs", {"ids": ["ap-living"]}),
    ):
        r = await c.post(path, json=body)  # no X-Illyhub header
        assert r.status_code == 403 and r.json()["code"] == "missing_header", path
        r = await c.post(
            path, json=body, headers={**HDR, "Origin": "https://evil.example", "Host": "localhost"}
        )
        assert r.status_code == 403 and r.json()["code"] == "forbidden_origin", path
    # GETs are unaffected.
    assert (await c.get("/api/airplay")).status_code == 200


async def test_settings_reports_airplay_info(client) -> None:
    c, rt = client
    hub = (await c.get("/api/settings")).json()["hub"]
    assert hub["airplay"] == {"enabled": True, "available": True, "reason": None}
    bridge(rt).unavailable_reason = "Automation permission for Music is missing."
    hub = (await c.get("/api/settings")).json()["hub"]
    assert hub["airplay"]["available"] is False and "Automation" in hub["airplay"]["reason"]


async def test_real_mode_without_flag_reports_pandora_sync_off_over_http(tmp_path: Path) -> None:
    """A real (non-fake) hub without HUB_AIRPLAY_ENABLED never touches osascript."""
    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path / "data"),
        app_dir=str(tmp_path / "no-app"),
        fonts_dir=str(tmp_path / "no-fonts"),
    )
    app = create_app(settings)
    rt: HubRuntime = app.state.runtime
    assert rt.airplay is not None and rt.airplay.bridge is None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
        body = (await c.get("/api/airplay")).json()
        assert body == {"enabled": False, "available": False, "reason": AIRPLAY_OFF, "outputs": []}
        r = await c.post("/api/pandora-sync/start", json={"output_ids": ["x"]}, headers=HDR)
        assert r.status_code == 503 and r.json()["error"]["code"] == "bridge_unavailable"
        assert r.json()["error"]["message"] == AIRPLAY_OFF
        assert "HUB_AIRPLAY_ENABLED" not in AIRPLAY_OFF
