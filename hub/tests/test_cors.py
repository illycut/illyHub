"""CORS for the Android shell (docs/android.md): the WebView origin is http://localhost, never the
hub's own, so the hub must answer preflights and expose responses to it while giving unlisted
origins nothing. The Origin check in api._origin_policy stays the write guard (tested elsewhere)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from illyhub_hub.api import SHELL_ORIGINS, create_app
from illyhub_hub.config import Settings


def _settings(tmp_path: Path, **kw) -> Settings:
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
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(_settings(tmp_path, allowed_origins=["https://hub.tail1234.ts.net"]))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c


@pytest.mark.parametrize("origin", SHELL_ORIGINS)
async def test_preflight_for_the_shell_origin_allows_the_restart_header(
    client: httpx.AsyncClient, origin: str
) -> None:
    r = await client.options(
        "/api/hub/restart",
        headers={
            "Origin": origin,
            "Host": "localhost",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, x-illyhub",
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers["access-control-allow-origin"] == origin
    allowed = {h.strip().lower() for h in r.headers["access-control-allow-headers"].split(",")}
    assert {"content-type", "x-illyhub"} <= allowed
    assert "POST" in r.headers["access-control-allow-methods"]
    assert "access-control-allow-credentials" not in r.headers


async def test_read_from_the_shell_origin_echoes_it_and_exposes_the_correlation_header(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/api/health", headers={"Origin": "http://localhost", "Host": "localhost"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost"
    assert "x-correlation-id" in r.headers.get("access-control-expose-headers", "").lower()


async def test_configured_extra_origin_is_allowed_too(client: httpx.AsyncClient) -> None:
    r = await client.get(
        "/api/health", headers={"Origin": "https://hub.tail1234.ts.net", "Host": "localhost"}
    )
    assert r.headers["access-control-allow-origin"] == "https://hub.tail1234.ts.net"


async def test_unlisted_origin_gets_no_cors_headers_but_reads_still_answer(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get(
        "/api/health", headers={"Origin": "https://evil.example", "Host": "localhost"}
    )
    assert r.status_code == 200  # reads are never blocked; the browser withholds the body
    assert "access-control-allow-origin" not in r.headers
    pre = await client.options(
        "/api/hub/restart",
        headers={
            "Origin": "https://evil.example",
            "Host": "localhost",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in pre.headers


async def test_cors_does_not_weaken_the_write_guard(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/browse/refresh", headers={"Origin": "https://evil.example", "Host": "localhost"}
    )
    assert r.status_code == 403 and r.json()["code"] == "forbidden_origin"
