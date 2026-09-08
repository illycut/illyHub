"""Serve the PWA static export and self-hosted fonts from the hub origin.

The Next.js export lives at ``HUB_APP_DIR`` (default ``../app/out``). It is served with SPA
fallback so deep links (``/now-playing``) resolve to the exported page or ``index.html``.
Reserved paths (``/api/...``, ``/ws``, ``/docs``, ``/redoc``, ``/openapi.json``) never fall back;
unknown paths on any HTTP method get the hub's 404 error envelope instead of FastAPI's default
``{"detail": ...}`` or a 405.

Cache headers: hashed ``_next/static`` assets and fonts are immutable for a year; HTML, the
service worker and the manifest are ``no-cache`` so a hub update is picked up on the next load.

Fonts (General Sans, Fontshare licence permits self-hosting) come from ``HUB_FONTS_DIR`` so the
app works when the internet is down but the LAN is up (design system §11).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.routing import Match, Route

from .logsetup import get_logger

log = get_logger("static")

IMMUTABLE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"
SHORT = "public, max-age=3600"
RESERVED_EXACT = frozenset({"api", "ws", "docs", "redoc", "openapi.json"})
RESERVED_PREFIX = "api/"
NO_CACHE_FILES = frozenset({"index.html", "sw.js", "service-worker.js", "manifest.webmanifest"})
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


@dataclass(frozen=True)
class StaticMounts:
    fonts: bool
    app: bool


def is_reserved(rel: str) -> bool:
    return rel in RESERVED_EXACT or rel.startswith(RESERVED_PREFIX)


def cache_header(rel: str) -> str:
    if rel.startswith("_next/static/"):
        return IMMUTABLE
    if rel.endswith(".html") or rel.rsplit("/", 1)[-1] in NO_CACHE_FILES:
        return NO_CACHE
    return SHORT


CATCH_ALL_PATH = "/{path:path}"


def not_found(path: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"code": "not_found", "message": f"No route for /{path.strip('/')}."},
    )


def allowed_methods(request: Request) -> list[str]:
    """Methods other routes accept for this path. Non-empty means the path exists and the
    request method is wrong, which deserves a 405 rather than the catch-all's 404."""
    allowed: set[str] = set()
    for route in request.app.router.routes:
        if isinstance(route, Route) and route.path != CATCH_ALL_PATH:
            match, _ = route.matches(request.scope)
            if match is Match.PARTIAL:
                allowed |= set(route.methods or ())
    return sorted(allowed)


def method_not_allowed(path: str, allowed: list[str]) -> JSONResponse:
    return JSONResponse(
        status_code=405,
        headers={"Allow": ", ".join(allowed)},
        content={
            "code": "method_not_allowed",
            "message": f"/{path.strip('/')} accepts {', '.join(allowed)}.",
        },
    )


def resolve_static(app_dir: Path, path: str) -> tuple[Path, str] | None:
    """Map a request path to a file inside ``app_dir``.

    Tries the exact file, ``{path}.html``, ``{path}/index.html``, then ``index.html`` (SPA
    fallback). Returns ``None`` for traversal attempts or when no index exists.
    """
    rel = path.strip("/")
    root = app_dir.resolve()
    candidates = [rel, f"{rel}.html", f"{rel}/index.html"] if rel else ["index.html"]
    for cand in candidates:
        target = (root / cand).resolve()
        if root not in target.parents:
            return None  # escaped the export directory
        if target.is_file():
            return target, cand
    index = root / "index.html"
    if index.is_file():
        return index, "index.html"
    return None


def mount_static(app: FastAPI, app_dir: Path, fonts_dir: Path) -> StaticMounts:
    """Register fonts, the SPA export (if present) and the any-method 404 catch-all."""
    fonts_ok = fonts_dir.is_dir()
    app_ok = app_dir.is_dir() and (app_dir / "index.html").is_file()
    if not fonts_ok:
        log.warning(
            "fonts dir missing; General Sans not served", extra={"extra": {"dir": str(fonts_dir)}}
        )
    if not app_ok:
        log.warning(
            "app export missing; PWA not served (build the app or set HUB_APP_DIR)",
            extra={"extra": {"dir": str(app_dir)}},
        )
    else:
        log.info("serving app export", extra={"extra": {"dir": str(app_dir)}})

    if fonts_ok:
        fonts_root = fonts_dir.resolve()

        @app.get("/fonts/{path:path}", include_in_schema=False)
        async def fonts(path: str) -> Response:
            target = (fonts_root / path).resolve()
            if fonts_root not in target.parents or not target.is_file():
                return not_found(f"fonts/{path}")
            return FileResponse(target, headers={"Cache-Control": IMMUTABLE})

    @app.api_route(CATCH_ALL_PATH, methods=ALL_METHODS, include_in_schema=False)
    async def catch_all(path: str, request: Request) -> Response:
        allowed = allowed_methods(request)
        if allowed:
            return method_not_allowed(path, allowed)
        if request.method not in ("GET", "HEAD") or is_reserved(path.strip("/")) or not app_ok:
            return not_found(path)
        found = resolve_static(app_dir, path)
        if found is None:
            return not_found(path)
        target, rel = found
        return FileResponse(target, headers={"Cache-Control": cache_header(rel)})

    return StaticMounts(fonts=fonts_ok, app=app_ok)
