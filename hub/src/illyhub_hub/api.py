"""FastAPI application factory.

Phase 0: ``GET /api/health``, ``GET /api/devices``. Phase 1: command endpoints under ``/api``
(transport, seek, skip, volume, mute, zone power, group), the ``/ws`` state stream, and the
fake-only ``/api/dev/fake/{scenario}`` scenario trigger. Phase 2: the art proxy
(``GET /api/art/{key}``) and static serving of the PWA export and fonts. See ``docs/api.md``.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator

from . import __version__
from .adapters.base import BaseAdapter
from .adapters.fake import FakeBundle
from .art import (
    KEY_RE,
    SIZES,
    ArtCache,
    ArtHelper,
    CacheStats,
    etag_matches,
    placeholder_variant,
)
from .coalesce import Coalescer
from .commands import Ack, CommandError, CommandRouter, ErrorEnvelope, http_status
from .config import Settings, Vendor
from .discovery import DiscoveredDevice, DiscoveryService, Registry, SearchFn
from .logsetup import correlation_id, get_logger
from .state import ConnectionStatus, Player, Side, StateStore, Zone
from .static import StaticMounts, mount_static
from .ws import ClientSession, DumpCache

log = get_logger("api")

HEOS_DISABLED_MSG = "HEOS adapter disabled until HUB_HEOS_HOST is set"
DENON_DISABLED_MSG = "Denon adapter disabled until HUB_DENON_HOST is set"
DEGRADED_STATES = {"reconnecting", "disconnected"}


class HealthResponse(BaseModel):
    status: str = Field(description="ok or degraded")
    degraded: bool
    fake_devices: bool
    version: str
    uptime_s: float
    connections: dict[str, ConnectionStatus]
    last_error: str | None = None
    discovery_last_run: str | None = None
    state_version: int
    static: dict[str, bool] = Field(default_factory=dict, description="fonts / app export served")
    art_cache: dict[str, int] | None = Field(
        default=None, description="entries, bytes, hits, misses; null when the proxy is off"
    )


class DevicesResponse(BaseModel):
    players: list[Player]
    zones: list[Zone]
    sides: list[Side]
    discovered: list[dict[str, Any]]


class TargetBody(BaseModel):
    target: str = Field(description="player id, side id, or 'all'")


class SeekBody(TargetBody):
    position_ms: int = Field(ge=0)


class SkipBody(TargetBody):
    delta_ms: int = Field(default=15_000, description="signed; ±15000 for the transport row")


class VolumeBody(BaseModel):
    target: str | None = None
    level: int | None = Field(default=None, ge=0, le=100)
    linked: bool = False
    delta: int | None = None

    @model_validator(mode="after")
    def _shape(self) -> VolumeBody:
        if self.linked:
            if self.level is None and self.delta is None:
                raise ValueError("linked volume needs level or delta")
        elif self.target is None or self.level is None:
            raise ValueError("volume needs target and level")
        return self


class MuteBody(TargetBody):
    muted: bool


class ZonePowerBody(BaseModel):
    zone_id: str = Field(description="Denon zone id, or a Sonos player/side id for stop+ungroup")
    on: bool


class GroupBody(BaseModel):
    vendor: Vendor
    coordinator_id: str
    member_ids: list[str]


class ScenarioResponse(BaseModel):
    scenario: str
    result: dict[str, Any]
    state_version: int


@dataclass
class HubRuntime:
    """Everything the app owns for its lifetime. Built by :func:`build_runtime`."""

    settings: Settings
    store: StateStore
    adapters: list[BaseAdapter]
    discovery: DiscoveryService
    router: CommandRouter
    fakes: FakeBundle | None = None
    heos_factory: Callable[..., Any] | None = None
    art: ArtCache | None = None
    started_at: float = field(default_factory=time.monotonic)

    async def start(self) -> None:
        if self.art is not None:
            await self.art.start()
        # Discovery runs in the background so uvicorn serves immediately instead of waiting
        # out the SSDP window.
        await self.discovery.start()
        if self.fakes:
            await self.fakes.start()
        else:
            for a in self.adapters:
                await a.connect()
        self.started_at = time.monotonic()

    async def stop(self) -> None:
        await self.discovery.stop()
        await self.router.aclose()
        if self.fakes:
            await self.fakes.stop()
        else:
            for a in self.adapters:
                await a.disconnect()
        if self.art is not None:
            await self.art.stop()
        await self.store.aclose()

    async def adopt_discovered(self, devices: list[DiscoveredDevice]) -> None:
        """Discovery → adapter handoff: the first HEOS device found becomes the HEOS host."""
        if self.fakes or any(a.name == "heos" for a in self.adapters):
            return
        heos = next((d for d in devices if d.vendor == "heos"), None)
        if heos is None:
            return
        from .adapters.heos import PyHeosAdapter, default_heos_factory

        adapter = PyHeosAdapter(
            self.store,
            self.settings,
            heos.ip,
            factory=self.heos_factory or default_heos_factory,
            art=ArtHelper(self.art),
        )
        self.adapters.append(adapter)
        self.router.register(adapter)
        log.info("heos host adopted from discovery", extra={"extra": {"host": heos.ip}})
        await adapter.connect()

    @property
    def uptime_s(self) -> float:
        return round(time.monotonic() - self.started_at, 1)


def build_runtime(
    settings: Settings,
    *,
    search: SearchFn | None = None,
    heos_factory: Callable[..., Any] | None = None,
    sonos_snapshot: Callable[[], Any] | None = None,
    art_transport: Any | None = None,
) -> HubRuntime:
    """Wire adapters from settings. Fakes when ``HUB_FAKE_DEVICES=1``, else real adapters.

    The keyword arguments exist so tests can inject a fake SSDP search, a fake pyheos client
    factory, a fake SoCo snapshot, and an httpx transport for the art proxy without touching
    the network.
    """
    store = StateStore()
    registry = Registry()
    discovery = DiscoveryService(
        settings, registry, search=search or (_noop_search if settings.fake_devices else None)
    )
    art_cache = build_art_cache(settings, store, art_transport)
    art = ArtHelper(art_cache)
    if settings.fake_devices:
        fakes = FakeBundle(store, tick_interval_s=settings.position_poll_s, art=art)
        router = CommandRouter(
            store,
            playback={"heos": fakes.heos, "sonos": fakes.sonos},
            denon=fakes.denon,
            coalescer=Coalescer(settings.command_coalesce_s),
        )
        return HubRuntime(
            settings, store, list(fakes.adapters), discovery, router, fakes=fakes, art=art_cache
        )

    from .adapters.denon import HttpDenonAdapter
    from .adapters.heos import PyHeosAdapter, default_heos_factory
    from .adapters.sonos import SoCoAdapter

    router = CommandRouter(store, coalescer=Coalescer(settings.command_coalesce_s))
    sonos = SoCoAdapter(store, settings, snapshot=sonos_snapshot, art=art)
    adapters: list[BaseAdapter] = [sonos]
    router.register(sonos)
    heos_host = settings.heos_host or settings.denon_host
    if heos_host:
        heos = PyHeosAdapter(
            store, settings, heos_host, factory=heos_factory or default_heos_factory, art=art
        )
        adapters.append(heos)
        router.register(heos)
    else:
        # Discovery hands the first HEOS device it finds to HubRuntime.adopt_discovered.
        store.set_connection("heos", "disabled", HEOS_DISABLED_MSG)
    if settings.denon_host:
        denon = HttpDenonAdapter(store, settings, settings.denon_host)
        adapters.append(denon)
        router.register(denon)
    else:
        store.set_connection("denon", "disabled", DENON_DISABLED_MSG)
    runtime = HubRuntime(
        settings, store, adapters, discovery, router, heos_factory=heos_factory, art=art_cache
    )
    if not heos_host:
        discovery.on_new_devices(runtime.adopt_discovered)
    return runtime


async def _noop_search() -> list[Any]:
    return []


def build_art_cache(
    settings: Settings, store: StateStore, transport: Any | None
) -> ArtCache | None:
    """The art proxy is on by default; ``HUB_ART_PROXY=0`` serves placeholders for every ref."""
    if not settings.art_proxy:
        return None
    return ArtCache(
        settings.art_path,
        ttl_days=settings.art_ttl_days,
        max_mb=settings.art_max_mb,
        fetch_timeout_s=settings.art_fetch_timeout_s,
        transport=transport,
        on_accent=store.update_art_accent,
        allow_fake=settings.fake_devices,
    )


def _art_stats(cache: ArtCache | None) -> dict[str, int] | None:
    if cache is None:
        return None
    stats: CacheStats = cache.stats()
    return {
        "entries": stats.entries,
        "bytes": stats.bytes,
        "hits": stats.hits,
        "misses": stats.misses,
    }


def _envelope(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def health_summary(
    connections: dict[str, ConnectionStatus], discovery_error: str | None
) -> tuple[bool, str | None]:
    """Degraded only for reconnecting/disconnected. ``last_error`` prefers active adapters."""
    degraded = any(c.state in DEGRADED_STATES for c in connections.values())
    ordered = sorted(connections.items(), key=lambda kv: (kv[1].state == "disabled", kv[0]))
    for _name, c in ordered:
        if c.last_error and c.state != "disabled":
            return degraded, c.last_error
    if discovery_error:
        return degraded, discovery_error
    for _name, c in ordered:
        if c.last_error:
            return degraded, c.last_error
    return degraded, None


def create_app(
    settings: Settings | None = None,
    *,
    runtime_factory: Callable[[Settings], HubRuntime] = build_runtime,
) -> FastAPI:
    settings = settings or Settings()
    runtime = runtime_factory(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info("hub starting", extra={"extra": {"fake_devices": settings.fake_devices}})
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()
            log.info("hub stopped")

    app = FastAPI(
        title="illyHub hub",
        version=__version__,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "system", "description": "Health and hub metadata"},
            {"name": "devices", "description": "Discovered players, zones, and sides"},
            {"name": "commands", "description": "Transport, seek, volume, mute, power, grouping"},
            {"name": "art", "description": "Proxied, cached, resized artwork with accent colour"},
            {"name": "dev", "description": "Fake-device scenario triggers (HUB_FAKE_DEVICES=1)"},
        ],
    )
    app.state.runtime = runtime
    app.state.static_mounted = None  # set once the catch-all is mounted, last
    router = runtime.router

    @app.exception_handler(CommandError)
    async def _command_error(_request: Request, exc: CommandError) -> JSONResponse:
        cid = correlation_id.get() or "-"
        ack = Ack(
            correlation_id=cid,
            ok=False,
            action="invalid",
            target=exc.target,
            state_version=runtime.store.state.version,
            error=ErrorEnvelope(
                code=exc.code, message=exc.message, target=exc.target, correlation_id=cid
            ),
        )
        return JSONResponse(status_code=http_status(exc.code), content=ack.model_dump(mode="json"))

    @app.middleware("http")
    async def _correlation(request: Request, call_next: Callable[[Request], Any]) -> Any:
        cid = request.headers.get("x-correlation-id") or uuid.uuid4().hex[:12]
        token = correlation_id.set(cid)
        try:
            response = await call_next(request)
        finally:
            correlation_id.reset(token)
        response.headers["x-correlation-id"] = cid
        return response

    @app.get("/api/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        state = runtime.store.state
        connections = dict(state.connections)
        for a in runtime.adapters:
            connections.setdefault(a.name, a.status())
        degraded, last_error = health_summary(connections, runtime.discovery.last_error)
        return HealthResponse(
            status="degraded" if degraded else "ok",
            degraded=degraded,
            fake_devices=settings.fake_devices,
            version=__version__,
            uptime_s=runtime.uptime_s,
            connections=connections,
            last_error=last_error,
            discovery_last_run=(
                runtime.discovery.last_run.isoformat() if runtime.discovery.last_run else None
            ),
            state_version=state.version,
            static=dict(vars(app.state.static_mounted)) if app.state.static_mounted else {},
            art_cache=_art_stats(runtime.art),
        )

    @app.get("/api/devices", response_model=DevicesResponse, tags=["devices"])
    async def devices() -> DevicesResponse:
        state = runtime.store.state
        return DevicesResponse(
            players=_sorted(state.players.values()),
            zones=_sorted(state.zones.values()),
            sides=_sorted(state.sides.values()),
            discovered=runtime.discovery.registry.dump(),
        )

    # -- commands (Phase 1) ---------------------------------------------------------

    def _respond(ack: Ack) -> JSONResponse:
        status = 200 if ack.ok else http_status(ack.error.code if ack.error else "vendor_error")
        return JSONResponse(status_code=status, content=ack.model_dump(mode="json"))

    @app.post("/api/transport/{action}", response_model=Ack, tags=["commands"])
    async def transport(
        action: Literal["play", "pause", "toggle", "stop", "next", "prev"], body: TargetBody
    ) -> JSONResponse:
        return _respond(await router.transport(body.target, action))

    @app.post("/api/seek", response_model=Ack, tags=["commands"])
    async def seek(body: SeekBody) -> JSONResponse:
        return _respond(await router.seek(body.target, body.position_ms))

    @app.post("/api/skip", response_model=Ack, tags=["commands"])
    async def skip(body: SkipBody) -> JSONResponse:
        return _respond(await router.skip(body.target, body.delta_ms))

    @app.post("/api/volume", response_model=Ack, tags=["commands"])
    async def volume(body: VolumeBody) -> JSONResponse:
        if body.linked:
            return _respond(await router.linked_volume(level=body.level, delta=body.delta))
        assert body.target is not None and body.level is not None  # validated by the model
        return _respond(await router.volume(body.target, body.level))

    @app.post("/api/mute", response_model=Ack, tags=["commands"])
    async def mute(body: MuteBody) -> JSONResponse:
        return _respond(await router.mute(body.target, body.muted))

    @app.post("/api/zone/power", response_model=Ack, tags=["commands"])
    async def zone_power(body: ZonePowerBody) -> JSONResponse:
        return _respond(await router.zone_power(body.zone_id, body.on))

    @app.post("/api/group", response_model=Ack, tags=["commands"])
    async def group(body: GroupBody) -> JSONResponse:
        return _respond(await router.group(body.vendor, body.coordinator_id, body.member_ids))

    @app.delete("/api/group/{side_id}", response_model=Ack, tags=["commands"])
    async def ungroup(side_id: str) -> JSONResponse:
        return _respond(await router.ungroup(side_id))

    # -- art proxy (Phase 2) --------------------------------------------------------

    @app.get("/api/art/{cache_key}", tags=["art"], response_class=Response)
    async def art(cache_key: str, request: Request, size: str = "320") -> Response:
        """Serve a cached variant.

        404 for an unknown key. Upstream failure (or a disabled proxy) returns **200** with a
        neutral placeholder PNG and ``X-Art-Fallback`` so an ``<img>`` never breaks.
        """
        if not KEY_RE.match(cache_key):
            return _envelope(400, "invalid_key", "cache_key must be 24 lowercase hex characters.")
        if size not in SIZES:
            return _envelope(400, "invalid_size", f"size must be one of {sorted(SIZES)}.")
        cache = runtime.art
        if cache is None:
            variant = placeholder_variant(cache_key, size, "proxy_disabled")
        else:
            variant = await cache.variant(cache_key, size)
            if variant is None:
                return _envelope(404, "unknown_art", "No artwork registered under that key.")
        if variant.fallback:
            return Response(
                content=variant.body,
                media_type=variant.media_type,
                headers={
                    "Cache-Control": "no-store",
                    "X-Art-Fallback": variant.fallback_reason or "upstream_error",
                },
            )
        headers = {"ETag": variant.etag, "Cache-Control": "public, max-age=2592000, immutable"}
        if etag_matches(request.headers.get("if-none-match"), variant.etag):
            return Response(status_code=304, headers=headers)
        assert variant.path is not None  # a non-fallback variant is always file-backed
        return FileResponse(variant.path, media_type=variant.media_type, headers=headers)

    # -- state stream ---------------------------------------------------------------

    dumps = DumpCache()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        session = ClientSession(websocket, runtime.store, dumps=dumps)
        await session.run(router)

    # -- fake scenarios -------------------------------------------------------------

    if runtime.fakes is not None:
        fakes = runtime.fakes

        @app.post("/api/dev/fake/{scenario}", response_model=ScenarioResponse, tags=["dev"])
        async def fake_scenario(scenario: str) -> JSONResponse:
            try:
                result = fakes.run_scenario(scenario)
            except KeyError:
                return JSONResponse(
                    status_code=404,
                    content={
                        "code": "unknown_scenario",
                        "message": f"Unknown scenario {scenario!r}.",
                        "scenarios": list(fakes.SCENARIOS),
                    },
                )
            return JSONResponse(
                content=ScenarioResponse(
                    scenario=scenario, result=result, state_version=runtime.store.state.version
                ).model_dump(mode="json")
            )

    # -- PWA export + fonts (must be last: catch-all GET) ---------------------------

    app.state.static_mounted = mount_static(app, settings.app_path, settings.fonts_path)
    assert isinstance(app.state.static_mounted, StaticMounts)

    return app


def _sorted(items: Sequence[Any] | Any) -> list[Any]:
    return sorted(items, key=lambda i: (getattr(i, "vendor", ""), i.name))
