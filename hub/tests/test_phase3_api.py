"""Phase 3 API: auth, browse, play, history, home, settings, restart — against the fakes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from illyhub_hub.api import HubRuntime, build_runtime, create_app, lan_address
from illyhub_hub.config import Settings


def fake_settings(tmp_path: Path, **kw) -> Settings:
    kw.setdefault("fake_tidal", True)
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


def flags(avail: dict) -> dict:
    """Vendor flags only; `reasons` are asserted where the reason matters."""
    return {"heos": avail["heos"], "sonos": avail["sonos"]}


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, HubRuntime]]:
    app = create_app(fake_settings(tmp_path))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c, app.state.runtime


ALBUM = {"service": "tidal", "kind": "album", "id": "101"}
PLAYLIST = {"service": "tidal", "kind": "playlist", "id": "p-2"}


async def test_auth_status_and_fake_link_toggle(client) -> None:
    c, rt = client
    st = (await c.get("/api/auth/tidal/status")).json()
    assert st["linked"] is True and st["account_name"] == "fake@tidal"
    r = await c.post("/api/auth/tidal/unlink")
    assert r.status_code == 200 and r.json()["linked"] is False
    r = await c.get("/api/browse/tidal/favorites/albums")
    assert r.status_code == 409
    assert r.json() == {
        "code": "needs_link",
        "message": "Tidal is not connected. Link it in Settings.",
        "service": "tidal",
    }
    home = (await c.get("/api/home")).json()
    assert home["playlists"] == {
        "items": [],
        "needs_link": ["tidal", "ytmusic"],
        "error": None,
        "linked": {"tidal": False, "ytmusic": False},
    }
    assert home["favorite_albums"]["needs_link"] == ["tidal", "ytmusic"] and home["recents"] == []
    assert (await c.get("/api/auth/tidal/status")).json()["state"] == "unlinked"
    rt.fakes.fake_link_delay_s = 0.05
    r = await c.post("/api/auth/tidal/start")
    assert r.status_code == 200 and r.json()["user_code"] == "FAKE1"
    st = (await c.get("/api/auth/tidal/status")).json()
    assert st["state"] == "pending" and st["linked"] is False and st["pending"]["user_code"]
    await asyncio.sleep(0.1)
    assert (await c.get("/api/auth/tidal/status")).json()["state"] == "linked"
    # approve_tidal completes a pending flow immediately
    await c.post("/api/auth/tidal/unlink")
    rt.fakes.fake_link_delay_s = 60
    await c.post("/api/auth/tidal/start")
    assert (await c.post("/api/dev/fake/approve_tidal")).json()["result"]["approved"] is True
    assert (await c.get("/api/auth/tidal/status")).json()["state"] == "linked"


async def test_browse_pages_and_containers(client) -> None:
    c, _rt = client
    page = (await c.get("/api/browse/tidal/favorites/albums?limit=4")).json()
    assert len(page["items"]) == 4 and page["next_offset"] == 4 and page["total"] == 6
    item = page["items"][0]
    assert item["content_ref"] == ALBUM and flags(item["availability"]) == {
        "heos": True,
        "sonos": True,
    }
    assert item["art"]["url"].startswith("/api/art/")
    album = (await c.get("/api/browse/tidal/album/101")).json()
    assert album["item"]["title"] == "Warm Glow" and len(album["tracks"]) == 9
    assert album["tracks"][2]["index"] == 2 and album["tracks"][2]["album_id"] == "101"
    pl = (await c.get("/api/browse/tidal/playlist/p-2")).json()
    assert pl["item"]["title"] == "Dinner party" and len(pl["tracks"]) == 6
    mine = (await c.get("/api/browse/tidal/playlists")).json()
    favs = (await c.get("/api/browse/tidal/favorites/playlists")).json()
    assert len(mine["items"]) == 3 and len(favs["items"]) == 1
    r = await c.get("/api/browse/tidal/album/999")
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert (await c.post("/api/browse/refresh")).json()["cleared"] >= 1


async def test_play_loads_queue_records_history_and_feeds_home(client) -> None:
    c, rt = client
    heos_side = "heos:heos-1"
    r = await c.post(
        "/api/play", json={"target": heos_side, "content_ref": ALBUM, "start_index": 2}
    )
    assert r.status_code == 200, r.text
    ack = r.json()
    assert ack["ok"] and ack["action"] == "play_content" and ack["applied"] == [heos_side]
    np = rt.store.state.now_playing[heos_side]
    assert np.title == "Sideband 3" and np.album == "Warm Glow" and np.source == "tidal"
    assert rt.store.state.sides[heos_side].play_state == "play"
    # next/prev walk the fake queue
    await c.post("/api/transport/next", json={"target": heos_side})
    assert rt.store.state.now_playing[heos_side].title == "Harmonic 4"
    await c.post("/api/transport/prev", json={"target": heos_side})
    assert rt.store.state.now_playing[heos_side].title == "Sideband 3"

    hist = (await c.get("/api/history")).json()["items"]
    assert len(hist) == 1 and hist[0]["content_ref"] == ALBUM
    assert hist[0]["last_targets"] == [heos_side] and hist[0]["title"] == "Warm Glow"

    # Play a playlist on all; home sorts it first by recency, then albums by recency.
    r = await c.post("/api/play", json={"target": "all", "content_ref": PLAYLIST})
    assert r.json()["ok"] and len(r.json()["applied"]) == 3
    home = (await c.get("/api/home")).json()
    assert [x["content_ref"]["id"] for x in home["recents"]] == ["p-2", "101"]
    assert home["playlists"]["items"][0]["content_ref"]["id"] == "p-2"
    rest = [x["title"] for x in home["playlists"]["items"][1:]]
    assert rest == sorted(rest, key=str.casefold)
    assert len(home["favorite_albums"]["items"]) == 6 and home["stations"]["items"] == []


async def test_play_errors_needs_link_side_unavailable_and_bad_index(client) -> None:
    c, rt = client
    r = await c.post(
        "/api/play", json={"target": "heos:heos-1", "content_ref": ALBUM, "start_index": 99}
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_argument"
    # HEOS app has no Tidal: that side fails, Sonos still plays.
    rt.fakes.heos.linked_services.discard("tidal")
    r = await c.post("/api/play", json={"target": "all", "content_ref": ALBUM})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert {p["code"] for p in body["partial"]} == {"not_available_on_side"}
    assert body["applied"] == [rt.fakes.sonos.side_id()]
    assert "link it in the heos app" in body["partial"][0]["message"].lower()
    # Hub account unlinked: needs_link before any side is touched.
    rt.fakes.set_tidal_linked(False)
    r = await c.post("/api/play", json={"target": "all", "content_ref": ALBUM})
    assert r.status_code == 409 and r.json()["code"] == "needs_link"
    r = await c.post(
        "/api/play", json={"target": "all", "content_ref": {**ALBUM, "service": "ytmusic"}}
    )
    assert r.status_code == 409 and r.json()["service"] == "ytmusic"
    r = await c.post("/api/play", json={"target": "nowhere", "content_ref": ALBUM})
    assert r.status_code == 409  # needs_link wins while unlinked
    rt.fakes.set_tidal_linked(True)
    r = await c.post("/api/play", json={"target": "nowhere", "content_ref": ALBUM})
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_target"


async def test_dev_scenarios_toggle_tidal(client) -> None:
    c, rt = client
    r = await c.post("/api/dev/fake/unlink_tidal")
    assert r.json()["result"] == {"tidal_linked": False}
    assert rt.fake_tidal.linked is False and "tidal" not in rt.fakes.sonos.linked_services
    r = await c.post("/api/dev/fake/link_tidal")
    assert r.json()["result"] == {"tidal_linked": True} and rt.fake_tidal.linked is True


async def test_settings_and_restart(client, monkeypatch: pytest.MonkeyPatch) -> None:
    c, rt = client
    body = (await c.get("/api/settings")).json()
    accounts = {a["service"]: a for a in body["accounts"]}
    assert set(accounts) == {"tidal", "ytmusic", "pandora", "heos_account"}
    assert accounts["tidal"]["linked"] is True and accounts["ytmusic"]["linked"] is False
    assert accounts["pandora"]["linked"] is True and accounts["heos_account"]["linked"] is True
    assert body["hub"]["port"] == 8080 and body["hub"]["https"] is False
    assert body["hub"]["fake_devices"] is True and body["hub"]["version"]
    kinds = {(h["vendor"], h["kind"]) for h in body["hardware"]}
    assert kinds == {("heos", "player"), ("sonos", "player"), ("denon", "zone")}
    assert len(body["hardware"]) == 6

    exits: list[int] = []
    rt.exit_fn = exits.append
    r = await c.post("/api/hub/restart")  # no X-Illyhub header: refused
    assert r.status_code == 403 and r.json()["code"] == "missing_header"
    r = await c.post("/api/hub/restart", headers={"X-Illyhub": "1"})
    assert r.status_code == 202 and r.json() == {"restarting": True}
    r = await c.post("/api/hub/restart", headers={"X-Illyhub": "1"})  # repeat: no second exit
    assert r.status_code == 202
    await asyncio.sleep(0.7)
    assert exits == [0]
    assert (await c.get("/api/health")).json()["history"] == {"ok": True, "last_error": None}
    assert (await c.get("/api/health")).json()["vault"] == {"ok": True, "last_error": None}


async def test_restart_can_be_disabled(tmp_path: Path) -> None:
    app = create_app(fake_settings(tmp_path, allow_restart=False))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as c:
            r = await c.post("/api/hub/restart", headers={"X-Illyhub": "1"})
            assert r.status_code == 409 and r.json()["code"] == "restart_disabled"


async def test_real_tidal_wiring_without_fake_tidal(tmp_path: Path) -> None:
    """HUB_FAKE_DEVICES without HUB_FAKE_TIDAL: the real TidalAuth/TidalService are wired, the
    vault is created, and everything reports 'not linked' without touching the network."""
    from test_tidal_auth import FakeSession

    settings = fake_settings(tmp_path, fake_tidal=False)
    rt = build_runtime(settings, tidal_session_factory=FakeSession)
    app = create_app(settings, runtime_factory=lambda _s: rt)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as c:
            await asyncio.sleep(0.02)  # restore() ran in the background: nothing stored
            st = (await c.get("/api/auth/tidal/status")).json()
            assert st["linked"] is False and st["pending"] is None and st["state"] == "unlinked"
            assert (await c.get("/api/browse/tidal/playlists")).status_code == 409
            home = (await c.get("/api/home")).json()
            assert home["playlists"]["needs_link"] == ["tidal", "ytmusic"]
            r = await c.post("/api/auth/tidal/start")
            assert r.status_code == 200 and r.json()["user_code"] == "ABCDE"
            assert (await c.get("/api/auth/tidal/status")).json()["pending"]["user_code"] == "ABCDE"
            assert (await c.post("/api/auth/tidal/unlink")).json()["linked"] is False
    assert (tmp_path / "data" / "vault.key").exists()


def test_lan_address_is_an_ipv4_string() -> None:
    addr = lan_address()
    assert addr.count(".") == 3


# -- CSRF / host policy (review blocker 1) ----------------------------------------------


async def test_cross_origin_state_changes_are_refused(client) -> None:
    c, rt = client
    evil = {"Origin": "http://evil.example"}
    r = await c.post("/api/auth/tidal/unlink", headers=evil)
    assert r.status_code == 403 and r.json()["code"] == "forbidden_origin"
    assert (await c.get("/api/auth/tidal/status")).json()["linked"] is True  # still linked
    exits: list[int] = []
    rt.exit_fn = exits.append
    r = await c.post("/api/hub/restart", headers={**evil, "X-Illyhub": "1"})
    assert r.status_code == 403
    await asyncio.sleep(0.6)
    assert exits == [] and rt.restarting is False
    r = await c.post("/api/dev/fake/unlink_tidal", headers=evil)
    assert r.status_code == 403 and rt.fakes.tidal_linked is True
    r = await c.post("/api/browse/refresh", headers=evil)
    assert r.status_code == 403
    # Reads are never blocked by Origin.
    assert (await c.get("/api/health", headers=evil)).status_code == 200


async def test_same_origin_and_originless_requests_pass(client) -> None:
    c, _rt = client
    for headers in (
        {"Origin": "http://localhost"},  # same-origin
        {"Origin": "http://127.0.0.1:8080"},  # the hub's own address on another port
        {"Origin": "https://hub.local"},  # *.local default
        {},  # curl / no Origin at all
    ):
        r = await c.post("/api/browse/refresh", headers=headers)
        assert r.status_code == 200, headers


async def test_configured_origins_and_hosts_are_honoured(tmp_path: Path) -> None:
    settings = fake_settings(
        tmp_path,
        allowed_hosts=["hub-mac.tail1234.ts.net"],
        allowed_origins=["https://custom.example:8443"],
    )
    app = create_app(settings, lan_ip="192.168.1.10")
    assert "192.168.1.10" in app.state.allowed_hosts and "custom.example" in app.state.allowed_hosts
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            ok = await c.post(
                "/api/browse/refresh", headers={"Origin": "https://hub-mac.tail1234.ts.net"}
            )
            assert ok.status_code == 200
            ok = await c.post(
                "/api/browse/refresh", headers={"Origin": "https://custom.example:8443"}
            )
            assert ok.status_code == 200
            bad = await c.post("/api/browse/refresh", headers={"Origin": "https://other.example"})
            assert bad.status_code == 403
        async with httpx.AsyncClient(transport=transport, base_url="http://rebound.attacker") as c:
            r = await c.get("/api/health")
            assert r.status_code == 400  # TrustedHostMiddleware


def test_host_and_origin_matching_rules() -> None:
    from illyhub_hub.api import host_allowed, origin_allowed

    hosts = ["localhost", "127.0.0.1", "*.local", "10.0.0.5"]
    assert host_allowed("HUB.LOCAL", hosts) and host_allowed("local", hosts) is True
    assert not host_allowed("evil.example", hosts) and not host_allowed("notlocal", hosts)
    s = Settings(_env_file=None, allowed_origins=["https://x.example"])
    assert origin_allowed("https://x.example", "hub", s, hosts)
    assert origin_allowed("http://hub:8080", "hub:8080", s, hosts)  # same-origin
    assert not origin_allowed("ftp://localhost", "hub", s, hosts)
    assert not origin_allowed("null", "hub", s, hosts)
    assert not origin_allowed("http://evil.example", None, s, hosts)


# -- vault failure and history health (review 3, 12) --------------------------------------


async def test_bad_vault_key_falls_back_to_memory_and_is_surfaced(tmp_path: Path) -> None:
    settings = fake_settings(tmp_path, fake_tidal=False, vault_key="definitely-not-a-fernet-key")
    from test_tidal_auth import FakeSession

    rt = build_runtime(settings, tidal_session_factory=FakeSession)
    assert rt.vault_error and "vault unavailable" in rt.vault_error
    assert rt.vault is not None and rt.vault.persistent is False
    app = create_app(settings, runtime_factory=lambda _s: rt)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as c:
            health = (await c.get("/api/health")).json()
            assert (
                health["vault"]["ok"] is False
                and "vault unavailable" in health["vault"]["last_error"]
            )
            accounts = {a["service"]: a for a in (await c.get("/api/settings")).json()["accounts"]}
            assert "vault unavailable" in accounts["tidal"]["last_error"]


async def test_home_section_failure_is_isolated(client) -> None:
    c, rt = client

    async def boom(limit: int, offset: int):
        raise RuntimeError("tidal is down")

    rt.fake_tidal.favorite_albums = boom  # type: ignore[method-assign]
    rt.tidal.refresh()
    await c.post("/api/play", json={"target": "heos:heos-1", "content_ref": ALBUM})
    home = (await c.get("/api/home")).json()
    assert home["favorite_albums"]["items"] == []
    assert "RuntimeError: tidal is down" in home["favorite_albums"]["error"]
    assert len(home["playlists"]["items"]) == 4 and len(home["recents"]) == 1  # others render


async def test_history_write_failure_is_reported_in_health(client, monkeypatch) -> None:
    import sqlite3

    c, rt = client

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(rt.history, "_record", broken)
    r = await c.post("/api/play", json={"target": "heos:heos-1", "content_ref": ALBUM})
    assert r.json()["ok"] is True  # playback still happened
    health = (await c.get("/api/health")).json()
    assert health["history"]["ok"] is False and "disk I/O error" in health["history"]["last_error"]
