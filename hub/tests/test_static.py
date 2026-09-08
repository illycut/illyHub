"""Static PWA export + fonts serving, the any-method 404 envelope, and TLS passthrough."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from illyhub_hub.api import create_app
from illyhub_hub.config import Settings, resolve_path
from illyhub_hub.main import uvicorn_kwargs
from illyhub_hub.static import StaticMounts, cache_header, is_reserved, resolve_static


def make_export(root: Path) -> Path:
    out = root / "out"
    (out / "_next" / "static" / "chunks").mkdir(parents=True)
    (out / "settings").mkdir()
    (out / "index.html").write_text("<html>index</html>")
    (out / "now-playing.html").write_text("<html>np</html>")
    (out / "settings" / "index.html").write_text("<html>settings</html>")
    (out / "_next" / "static" / "chunks" / "app-abc123.js").write_text("console.log(1)")
    (out / "manifest.webmanifest").write_text("{}")
    (out / "sw.js").write_text("self.addEventListener('fetch', () => {})")
    (out / "icon.png").write_bytes(b"\x89PNG")
    return out


def test_resolve_static_candidates_and_traversal(tmp_path: Path) -> None:
    out = make_export(tmp_path)
    assert resolve_static(out, "")[1] == "index.html"
    assert resolve_static(out, "now-playing")[1] == "now-playing.html"
    assert resolve_static(out, "settings")[1] == "settings/index.html"
    assert resolve_static(out, "_next/static/chunks/app-abc123.js")[1].endswith(".js")
    assert resolve_static(out, "deep/link/nowhere")[1] == "index.html"  # SPA fallback
    assert resolve_static(out, "../pyproject.toml") is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert resolve_static(empty, "anything") is None


def test_reserved_and_cache_header_rules() -> None:
    assert is_reserved("api") and is_reserved("api/health") and is_reserved("ws")
    assert is_reserved("docs") and is_reserved("openapi.json") and is_reserved("redoc")
    assert not is_reserved("ws-guide") and not is_reserved("apis") and not is_reserved("docsy")
    assert cache_header("_next/static/chunks/x.js").endswith("immutable")
    assert (
        cache_header("index.html") == "no-cache" and cache_header("now-playing.html") == "no-cache"
    )
    for f in ("sw.js", "service-worker.js", "manifest.webmanifest"):
        assert cache_header(f) == "no-cache"
    assert cache_header("icon.png") == "public, max-age=3600"


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        _env_file=None,
        fake_devices=True,
        position_poll_s=0.02,
        data_dir=str(tmp_path / "data"),
        app_dir=str(tmp_path / "out"),
        fonts_dir=str(tmp_path / "fonts"),
    )
    base.update(overrides)
    return Settings(**base)


async def test_serves_export_with_cache_headers_and_reserved_paths(tmp_path: Path) -> None:
    make_export(tmp_path)
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts" / "GeneralSans-Medium.woff2").write_bytes(b"wOF2")
    app = create_app(_settings(tmp_path))
    assert app.state.static_mounted == StaticMounts(fonts=True, app=True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub"
        ) as c:
            r = await c.get("/")
            assert r.status_code == 200 and "index" in r.text
            assert r.headers["cache-control"] == "no-cache"
            r = await c.get("/now-playing")
            assert "np" in r.text and r.headers["cache-control"] == "no-cache"
            assert "settings" in (await c.get("/settings")).text
            r = await c.get("/_next/static/chunks/app-abc123.js")
            assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
            r = await c.get("/sw.js")
            assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
            r = await c.get("/manifest.webmanifest")
            assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
            r = await c.get("/icon.png")
            assert r.headers["cache-control"] == "public, max-age=3600"
            r = await c.get("/some/deep/route")
            assert r.status_code == 200 and "index" in r.text  # SPA fallback
            r = await c.get("/ws-guide")
            assert r.status_code == 200 and "index" in r.text  # not reserved: only exact "ws"
            r = await c.get("/fonts/GeneralSans-Medium.woff2")
            assert r.status_code == 200 and r.content == b"wOF2"
            assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
            r = await c.get("/fonts/missing.woff2")
            assert r.status_code == 404 and r.json()["code"] == "not_found"
            assert (await c.get("/api/health")).status_code == 200  # API still wins
            assert (await c.get("/..%2Fpyproject.toml")).status_code == 404


async def test_unknown_paths_get_404_envelope_on_any_method(tmp_path: Path) -> None:
    make_export(tmp_path)
    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub"
        ) as c:
            for method, path in (
                ("GET", "/api/nope"),
                ("GET", "/api"),
                ("POST", "/api/nonexistent"),
                ("PUT", "/whatever"),
                ("DELETE", "/api/art/zzz/extra"),
            ):
                r = await c.request(method, path)
                assert r.status_code == 404, (method, path, r.status_code)
                body = r.json()
                assert body["code"] == "not_found" and body["message"].startswith("No route for /")
            # a real route with the wrong method is a 405 envelope, not swallowed into a 404
            r = await c.delete("/api/health")
            assert r.status_code == 405 and r.json()["code"] == "method_not_allowed"
            assert r.headers["allow"] == "GET"
            r = await c.get("/api/seek")
            assert r.status_code == 405 and r.headers["allow"] == "POST"


async def test_missing_export_is_not_mounted_but_404s_are_enveloped(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    assert app.state.static_mounted == StaticMounts(fonts=False, app=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub"
        ) as c:
            r = await c.get("/")
            assert r.status_code == 404 and r.json()["code"] == "not_found"
            r = await c.post("/api/nonexistent")
            assert r.status_code == 404 and r.json()["code"] == "not_found"
            assert (await c.get("/fonts/x.woff2")).status_code == 404
            health = (await c.get("/api/health")).json()
            assert health["static"] == {"fonts": False, "app": False}


def test_uvicorn_kwargs_tls(tmp_path: Path) -> None:
    plain = _settings(tmp_path)
    kw = uvicorn_kwargs(plain)
    assert kw["host"] == plain.host and kw["port"] == plain.port and "ssl_certfile" not in kw

    cert = tmp_path / "hub.pem"
    key = tmp_path / "hub-key.pem"
    cert.write_text("cert")
    key.write_text("key")
    kw = uvicorn_kwargs(_settings(tmp_path, tls_cert=str(cert), tls_key=str(key)))
    assert kw["ssl_certfile"] == str(cert) and kw["ssl_keyfile"] == str(key)

    with pytest.raises(ValueError, match="set together"):
        uvicorn_kwargs(_settings(tmp_path, tls_cert=str(cert)))
    with pytest.raises(ValueError, match="not a file"):
        uvicorn_kwargs(_settings(tmp_path, tls_cert=str(cert), tls_key=str(tmp_path / "nope")))

    relative = _settings(tmp_path, tls_cert="tls/hub.pem", tls_key="tls/hub-key.pem")
    assert relative.tls_cert_path == resolve_path("tls/hub.pem")
    assert relative.tls_cert_path.parent.parent.name == "hub"


def test_relative_paths_resolve_against_hub_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("HUB_DATA_DIR", "HUB_APP_DIR", "HUB_FONTS_DIR"):
        monkeypatch.delenv(var, raising=False)  # exercise the real defaults
    s = Settings(_env_file=None)
    assert s.data_path == resolve_path("data") and s.data_path.name == "data"
    assert s.data_path.parent.name == "hub" and s.art_path.name == "art"
    assert s.app_path.parts[-2:] == ("app", "out") and s.fonts_path.name == "fonts"
    assert resolve_path(str(tmp_path)) == tmp_path
    assert s.tls_cert_path is None and s.tls_key_path is None
