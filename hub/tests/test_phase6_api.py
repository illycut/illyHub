"""Phase 6 API: YouTube Music auth, browse, home merge, Sonos-only play, scenarios, HEOS stub."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from illyhub_hub.api import HubRuntime, create_app
from illyhub_hub.config import Settings

HEOS_SIDE = "heos:heos-1"
SONOS_SIDE = "sonos:sonos-gRINCON_KITCHEN:1"


def fake_settings(tmp_path: Path, **kw) -> Settings:
    kw.setdefault("fake_tidal", True)
    kw.setdefault("fake_ytmusic", True)
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
    app = create_app(fake_settings(tmp_path))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c, app.state.runtime


def flags(avail: dict) -> dict:
    """Vendor flags only; `reasons` are asserted where the reason matters."""
    return {"heos": avail["heos"], "sonos": avail["sonos"]}


def ref(kind: str, id_: str) -> dict[str, str]:
    return {"service": "ytmusic", "kind": kind, "id": id_}


async def test_browse_endpoints_and_availability(client) -> None:
    c, _rt = client
    pls = (await c.get("/api/browse/ytmusic/playlists")).json()
    assert [i["title"] for i in pls["items"]] == ["Morning run", "Kitchen dancing", "Liked Music"]
    # HEOS has no YouTube Music source; Sonos has it linked in fake mode.
    assert flags(pls["items"][0]["availability"]) == {"heos": False, "sonos": True}
    albums = (await c.get("/api/browse/ytmusic/albums")).json()
    assert [i["content_ref"]["id"] for i in albums["items"]][:1] == ["MPREb_alb1"]
    album = (await c.get("/api/browse/ytmusic/album/MPREb_alb1")).json()
    assert album["item"]["title"] == "Paper Lanterns" and len(album["tracks"]) == 8
    assert album["tracks"][2]["index"] == 2 and album["tracks"][2]["album_id"] == "MPREb_alb1"
    pl = (await c.get("/api/browse/ytmusic/playlist/PLyt001")).json()
    assert len(pl["tracks"]) == 6
    assert (await c.get("/api/browse/ytmusic/album/MPREb_nope")).status_code == 404


async def test_home_merges_ytmusic_playlists_and_albums_with_badges(client) -> None:
    c, _rt = client
    home = (await c.get("/api/home")).json()
    services = {i["content_ref"]["service"] for i in home["playlists"]["items"]}
    assert services == {"tidal", "ytmusic"}
    titles = [i["title"] for i in home["playlists"]["items"]]
    assert titles == sorted(titles, key=str.casefold)  # nothing played yet: alphabetical
    assert "Morning run" in titles and "Sunday morning" in titles
    albums = home["favorite_albums"]["items"]
    assert {i["content_ref"]["service"] for i in albums} == {"tidal", "ytmusic"} and len(
        albums
    ) == 9
    album_titles = [i["title"] for i in albums]
    assert album_titles == sorted(album_titles, key=str.casefold)  # one sort across services
    assert home["playlists"]["needs_link"] == []
    assert home["playlists"]["linked"] == {"tidal": True, "ytmusic": True}


async def test_play_ytmusic_on_sonos_ok_heos_unavailable_and_history_availability(client) -> None:
    c, rt = client
    r = await c.post(
        "/api/play", json={"target": SONOS_SIDE, "content_ref": ref("playlist", "PLyt001")}
    )
    assert r.status_code == 200, r.text
    ack = r.json()
    assert ack["ok"] and ack["applied"] == [SONOS_SIDE] and ack["partial"] == []
    np = rt.store.state.now_playing[SONOS_SIDE]
    assert np.source == "ytmusic" and np.title == "Lantern 1"
    assert np.content_ref is not None and np.content_ref.service == "ytmusic"

    r = await c.post(
        "/api/play", json={"target": HEOS_SIDE, "content_ref": ref("album", "MPREb_alb2")}
    )
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False and body["error"]["code"] == "not_available_on_side"
    assert body["error"]["message"] == "YouTube Music isn't available on HEOS."

    r = await c.post("/api/play", json={"target": "all", "content_ref": ref("album", "MPREb_alb2")})
    ack = r.json()
    assert ack["ok"] and ack["applied"] == [SONOS_SIDE]
    codes = {p["target"]: p["code"] for p in ack["partial"]}
    assert all(code == "not_available_on_side" for code in codes.values())
    assert HEOS_SIDE in codes

    hist = (await c.get("/api/history")).json()["items"]
    assert hist[0]["content_ref"] == ref("album", "MPREb_alb2")
    assert flags(hist[0]["availability"]) == {"heos": False, "sonos": True}
    home = (await c.get("/api/home")).json()
    # The just-played YouTube Music album leads the recents rail.
    assert home["recents"][0]["content_ref"]["id"] == "MPREb_alb2"
    assert home["playlists"]["items"][0]["title"] == "Morning run"  # recency of play first


async def test_sync_play_refuses_ytmusic(client) -> None:
    c, _rt = client
    r = await c.post(
        "/api/sync/play",
        json={
            "content_ref": ref("album", "MPREb_alb1"),
            "heos_target": HEOS_SIDE,
            "sonos_target": SONOS_SIDE,
        },
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "unsupported_content"


async def test_account_status_unlink_relink_and_connect_state(client) -> None:
    c, rt = client
    status = (await c.get("/api/auth/ytmusic/status")).json()
    assert (
        status["linked"]
        and status["state"] == "linked"
        and status["account_name"] == "fake@ytmusic"
    )
    settings = (await c.get("/api/settings")).json()
    yt_row = next(a for a in settings["accounts"] if a["service"] == "ytmusic")
    assert yt_row["linked"] is True

    r = await c.post("/api/auth/ytmusic/unlink")
    assert r.json()["state"] == "unlinked"
    r = await c.get("/api/browse/ytmusic/playlists")
    assert r.status_code == 409
    assert r.json() == {
        "code": "needs_link",
        "service": "ytmusic",
        "message": "YouTube Music is not connected. Link it in Settings.",
    }
    home = (await c.get("/api/home")).json()
    # Tidal still renders; YouTube Music simply drops out of the merged sections.
    assert {i["content_ref"]["service"] for i in home["playlists"]["items"]} == {"tidal"}
    assert home["playlists"]["needs_link"] == ["ytmusic"]
    assert home["playlists"]["linked"] == {"tidal": True, "ytmusic": False}
    # Sonos lost the account too: playing a YouTube Music ref is refused before any side.
    r = await c.post(
        "/api/play", json={"target": SONOS_SIDE, "content_ref": ref("album", "MPREb_alb1")}
    )
    assert r.status_code == 409 and r.json()["code"] == "needs_link"

    assert rt.fakes is not None
    rt.fakes.fake_link_delay_s = 30  # stays pending until approved
    r = await c.post("/api/auth/ytmusic/start")
    assert r.status_code == 200 and r.json()["user_code"] == "FAKE-YT-1"
    assert (await c.get("/api/auth/ytmusic/status")).json()["state"] == "pending"
    r = await c.post("/api/dev/fake/approve_ytmusic")
    assert r.json()["result"] == {"ytmusic_linked": True, "approved": True}
    assert (await c.get("/api/auth/ytmusic/status")).json()["state"] == "linked"
    assert (await c.get("/api/browse/ytmusic/playlists")).status_code == 200


async def test_link_scenarios_and_refresh(client) -> None:
    c, _rt = client
    r = await c.post("/api/dev/fake/unlink_ytmusic")
    assert r.json()["result"] == {"ytmusic_linked": False}
    r = await c.post("/api/dev/fake/link_ytmusic")
    assert r.json()["result"] == {"ytmusic_linked": True}
    cleared = (await c.post("/api/browse/refresh")).json()["cleared"]
    assert cleared >= 0
    await asyncio.sleep(0)


async def test_heos_stream_stub_is_501_with_spike_pointer(client) -> None:
    c, _rt = client
    r = await c.get("/api/stream/ytmusic/dQw4w9WgXcQ")
    assert r.status_code == 501
    body = r.json()
    assert body["code"] == "not_implemented" and "ytmusic-heos.md" in body["message"]


async def test_without_fake_ytmusic_the_account_is_unlinked_and_needs_configuration(
    tmp_path: Path,
) -> None:
    app = create_app(fake_settings(tmp_path, fake_ytmusic=False))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            status = (await c.get("/api/auth/ytmusic/status")).json()
            assert status["linked"] is False and status["state"] == "unlinked"
            assert "HUB_YTMUSIC_CLIENT_ID" in status["last_error"]
            assert status["last_error_code"] == "needs_client_config"
            r = await c.post("/api/auth/ytmusic/start")
            assert r.status_code == 409 and r.json()["code"] == "needs_client_config"
            assert r.json()["service"] == "ytmusic"
            home = (await c.get("/api/home")).json()
            assert {i["content_ref"]["service"] for i in home["playlists"]["items"]} == {"tidal"}
            # The fake Sonos side has no YouTube Music account either in this mode.
            assert app.state.runtime.router.service_availability("ytmusic").sonos is False
