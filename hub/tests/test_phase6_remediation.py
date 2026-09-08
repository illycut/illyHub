"""Phase 6 review remediation: single-service home configurations, URI parsing, caps, refresh
failure over HTTP, vendor-unsupported before connection checks."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from illyhub_hub.adapters.sonos import ytmusic_ref_from_uri
from illyhub_hub.api import HubRuntime, build_runtime, create_app
from illyhub_hub.art import ArtHelper
from illyhub_hub.content import Availability, ContentRef
from illyhub_hub.services.tidal import TidalService
from illyhub_hub.services.ytmusic import YTMusicCatalog
from test_phase6_api import HEOS_SIDE, SONOS_SIDE, fake_settings
from test_ytmusic_auth import FakeCreds, make
from test_ytmusic_service import FakeYT, service


async def _client(app) -> AsyncIterator[tuple[httpx.AsyncClient, HubRuntime]]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c, app.state.runtime


@pytest.fixture
async def client(tmp_path: Path):
    async for pair in _client(create_app(fake_settings(tmp_path))):
        yield pair


# -- blocker 1: single-service households ------------------------------------------------------


async def test_home_matrix_yt_only_tidal_only_both_neither(client) -> None:
    c, rt = client
    fakes = rt.fakes
    assert fakes is not None

    async def home() -> dict[str, Any]:
        return (await c.get("/api/home")).json()

    # both linked
    h = await home()
    for key in ("playlists", "favorite_albums"):
        assert h[key]["needs_link"] == [] and h[key]["linked"] == {"tidal": True, "ytmusic": True}
        assert {i["content_ref"]["service"] for i in h[key]["items"]} == {"tidal", "ytmusic"}

    # YouTube Music only: Tidal unlinked must not blank the sections
    fakes.set_tidal_linked(False)
    rt.tidal.refresh()
    h = await home()
    for key in ("playlists", "favorite_albums"):
        assert h[key]["needs_link"] == ["tidal"]
        assert h[key]["linked"] == {"tidal": False, "ytmusic": True}
        assert h[key]["items"] and {i["content_ref"]["service"] for i in h[key]["items"]} == {
            "ytmusic"
        }
        assert h[key]["error"] is None
    titles = [i["title"] for i in h["playlists"]["items"]]
    assert titles == sorted(titles, key=str.casefold)

    # Tidal only
    fakes.set_tidal_linked(True)
    fakes.set_ytmusic_linked(False)
    h = await home()
    for key in ("playlists", "favorite_albums"):
        assert h[key]["needs_link"] == ["ytmusic"]
        assert h[key]["linked"] == {"tidal": True, "ytmusic": False}
        assert {i["content_ref"]["service"] for i in h[key]["items"]} == {"tidal"}

    # neither
    fakes.set_tidal_linked(False)
    rt.tidal.refresh()
    h = await home()
    for key in ("playlists", "favorite_albums"):
        assert h[key]["items"] == [] and h[key]["needs_link"] == ["tidal", "ytmusic"]
        assert h[key]["linked"] == {"tidal": False, "ytmusic": False}


async def test_home_one_service_failing_is_null_linked_and_others_render(client) -> None:
    c, rt = client
    assert rt.fake_ytmusic is not None

    async def boom(limit: int, offset: int):
        raise RuntimeError("yt is down")

    rt.fake_ytmusic.user_playlists = boom  # type: ignore[method-assign]
    rt.ytmusic.refresh()
    h = (await c.get("/api/home")).json()
    pl = h["playlists"]
    assert pl["linked"] == {"tidal": True, "ytmusic": None} and pl["needs_link"] == []
    assert {i["content_ref"]["service"] for i in pl["items"]} == {"tidal"}
    assert pl["error"] is None  # Tidal still produced items, so the section is not "in error"
    # albums untouched
    assert h["favorite_albums"]["linked"] == {"tidal": True, "ytmusic": True}


# -- 2: URI parsing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uri, expected",
    [
        ("x-sonos-http:sonos-track:dQw4w9WgXcQ.mp4?sid=284&flags=8232&sn=4", "dQw4w9WgXcQ"),
        ("x-sonos-http:sonos-track%3adQw4w9WgXcQ.mp4?sid=284&sn=4", "dQw4w9WgXcQ"),
        ("x-sonos-http:sonos-track%3AdQw4w9WgXcQ.mp4?sid=284&sn=4", "dQw4w9WgXcQ"),
        ("x-sonosapi-hls-static:dQw4w9WgXcQ?sid=284&flags=8&sn=4", "dQw4w9WgXcQ"),
        ("x-sonos-http:sonos-track:short.mp4?sid=284&sn=4", None),  # too short, never "sonos-track"
        ("x-sonos-http:sonos-track:dQw4w9WgXcQ.mp4?sid=2840&sn=4", None),  # sid must be exact
        ("x-sonos-http:sonos-track:dQw4w9WgXcQ.mp4?flags=8232&sn=4", None),  # no sid
        ("x-sonos-http:sonos-track:dQw4w9WgXcQtoolong.mp4?sid=284&sn=4", None),  # id not terminated
        ("x-sonos-http:track/12345.flac?sid=174&sn=3", None),
    ],
)
def test_ytmusic_ref_from_uri_cases(uri: str, expected: str | None) -> None:
    ref = ytmusic_ref_from_uri(uri)
    if expected is None:
        assert ref is None
    else:
        assert ref == ContentRef(service="ytmusic", kind="track", id=expected)


# -- 3 / 4 / 11: album availability filter, caps, tolerant fixtures --------------------------


class CappedYT(FakeYT):
    def get_album(self, browse_id: str) -> dict:
        a = super().get_album(browse_id)
        a["tracks"] = [
            {
                "videoId": f"vid{i:08d}",
                "title": f"T{i}",
                "artists": [{"name": "X"}],
                "album": "Paper Lanterns",  # album as a plain string on get_album rows
                "duration_seconds": 100,
                "isAvailable": i != 3,  # one unavailable row
            }
            for i in range(12)
        ]
        return a

    def get_playlist(self, playlist_id: str, limit: int | None = 100, **kw: Any) -> dict:
        self.requested_limit = limit
        p = super().get_playlist(playlist_id, limit, **kw)
        p["tracks"] = [
            {"videoId": f"vid{i:08d}", "title": f"P{i}", "artists": [], "isAvailable": True}
            for i in range(min(limit or 100, 40))
        ]
        return p

    def get_library_playlists(self, limit: int | None = 25) -> list[dict]:
        self.library_limit = limit
        self._guard("playlists")
        return [{"title": "Bare", "playlistId": "PLbare", "thumbnails": []}]  # no count/author


async def test_album_filters_unavailable_and_caps_with_truncated_flag() -> None:
    yt = CappedYT()
    catalog = YTMusicCatalog(lambda: yt, ArtHelper(), max_tracks=8, max_library_items=50)
    svc = TidalService(
        catalog,
        is_linked=lambda: True,
        availability=lambda: Availability(sonos=True),
        service="ytmusic",
    )
    c = await svc.container(ContentRef(service="ytmusic", kind="album", id="MPREb_A1"))
    ids = [t.content_ref.id for t in c.tracks]
    assert "vid00000003" not in ids  # isAvailable false dropped
    assert len(ids) == 8 and c.truncated is True  # 11 playable → cut to 8
    assert [t.index for t in c.tracks] == list(range(8))
    assert c.tracks[0].album == "Paper Lanterns"  # string album honoured
    p = await svc.container(ContentRef(service="ytmusic", kind="playlist", id="PLrun"))
    assert yt.requested_limit == 9  # cap + 1 so a full page is distinguishable
    assert len(p.tracks) == 8 and p.truncated is True
    pls = await svc.user_playlists()
    assert yt.library_limit == 50
    assert pls.items[0].title == "Bare" and pls.items[0].subtitle is None
    assert pls.items[0].track_count is None


async def test_small_library_is_not_truncated() -> None:
    svc, _yt = service()
    c = await svc.container(ContentRef(service="ytmusic", kind="album", id="MPREb_A1"))
    assert c.truncated is False and len(c.tracks) == 2


# -- 5 / 12: refresh failure surfaces over HTTP as needs_link ----------------------------------


async def test_refresh_failure_mid_browse_is_409_needs_link_over_http(tmp_path: Path) -> None:
    creds = FakeCreds(refresh_ok=False)
    yt = FakeYT()
    app = create_app(
        fake_settings(
            tmp_path, fake_ytmusic=False, ytmusic_client_id="cid", ytmusic_client_secret="sec"
        ),
        runtime_factory=lambda s: build_runtime(
            s,
            ytmusic_credentials_factory=lambda cid, sec: creds,
            ytmusic_client_factory=lambda token, c: yt,
        ),
    )
    async for c, rt in _client(app):
        auth = rt.ytmusic_auth
        assert auth is not None
        await auth.start_link()
        async with asyncio.timeout(1):
            while not auth.linked:
                await asyncio.sleep(0.005)
        assert (await c.get("/api/browse/ytmusic/playlists")).status_code == 200
        rt.ytmusic.refresh()
        assert auth.token is not None
        auth.token["expires_at"] = int(time.time()) + 10  # inside the refresh margin
        r = await c.get("/api/browse/ytmusic/playlists")
        assert r.status_code == 409
        assert r.json() == {
            "code": "needs_link",
            "service": "ytmusic",
            "message": "YouTube Music session expired. Link it again.",
        }
        st = (await c.get("/api/auth/ytmusic/status")).json()
        assert st["state"] == "unlinked" and st["last_error"] == r.json()["message"]
        assert rt.vault is not None and rt.vault.get("ytmusic") is None


async def test_slow_down_grows_the_polling_interval(tmp_path: Path) -> None:
    auth, _vault, creds, _ = make(tmp_path, FakeCreds(pending=2, slow_down_first=True))
    auth.slow_down_step_s = 0.2
    stamps: list[float] = []
    orig = creds.token_from_code

    def stamped(code: str) -> dict[str, Any]:
        stamps.append(time.monotonic())
        return orig(code)

    creds.token_from_code = stamped  # type: ignore[method-assign]
    await auth.start_link()
    async with asyncio.timeout(3):
        while not auth.linked:
            await asyncio.sleep(0.005)
    assert len(stamps) == 3
    # first gap ≈ interval (0.01), later gaps ≈ interval + step (0.21)
    assert stamps[2] - stamps[1] > 0.15 and stamps[1] - stamps[0] > 0.15
    await auth.aclose()


# -- 6: vendor-unsupported before the connection check -----------------------------------------


async def test_heos_reconnecting_still_says_not_available_for_ytmusic(client) -> None:
    c, rt = client
    assert rt.fakes is not None
    rt.fakes.heos.drop("cable pull")
    r = await c.post(
        "/api/play",
        json={
            "target": HEOS_SIDE,
            "content_ref": {"service": "ytmusic", "kind": "album", "id": "MPREb_alb1"},
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "not_available_on_side"
    assert r.json()["error"]["message"] == "YouTube Music isn't available on HEOS."
    rt.fakes.heos.restore()


# -- 12: empty playlist after filtering -------------------------------------------------------


async def test_playlist_with_no_playable_tracks_is_invalid_argument(tmp_path: Path) -> None:
    class EmptyYT(FakeYT):
        def get_playlist(self, playlist_id: str, limit: int | None = 100, **kw: Any) -> dict:
            p = super().get_playlist(playlist_id, limit, **kw)
            p["tracks"] = [{"videoId": "vid00000009", "title": "Gone", "isAvailable": False}]
            return p

    yt = EmptyYT()
    creds = FakeCreds()
    app = create_app(
        fake_settings(
            tmp_path, fake_ytmusic=False, ytmusic_client_id="cid", ytmusic_client_secret="sec"
        ),
        runtime_factory=lambda s: build_runtime(
            s,
            ytmusic_credentials_factory=lambda cid, sec: creds,
            ytmusic_client_factory=lambda token, c: yt,
        ),
    )
    async for c, rt in _client(app):
        auth = rt.ytmusic_auth
        assert auth is not None
        await auth.start_link()
        async with asyncio.timeout(1):
            while not auth.linked:
                await asyncio.sleep(0.005)
        # the fake Sonos side needs the account for the play to reach the router
        assert rt.fakes is not None
        rt.fakes.set_ytmusic_linked(True)
        r = await c.post(
            "/api/play",
            json={
                "target": SONOS_SIDE,
                "content_ref": {"service": "ytmusic", "kind": "playlist", "id": "PLrun"},
            },
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_argument"
        assert r.json()["error"]["message"] == "There is nothing to play."
