"""FastAPI application factory. Phase 0 exposes ``/api/health`` and ``/api/devices``."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from . import __version__
from .adapters.base import BaseAdapter
from .adapters.fake import FakeBundle
from .config import Settings
from .discovery import DiscoveryService, Registry
from .logsetup import correlation_id, get_logger
from .state import ConnectionStatus, Player, Side, StateStore, Zone

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


class DevicesResponse(BaseModel):
    players: list[Player]
    zones: list[Zone]
    sides: list[Side]
    discovered: list[dict[str, Any]]


@dataclass
class HubRuntime:
    """Everything the app owns for its lifetime. Built by :func:`build_runtime`."""

    settings: Settings
    store: StateStore
    adapters: list[BaseAdapter]
    discovery: DiscoveryService
    fakes: FakeBundle | None = None
    started_at: float = field(default_factory=time.monotonic)

    async def start(self) -> None:
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
        if self.fakes:
            await self.fakes.stop()
        else:
            for a in self.adapters:
                await a.disconnect()
        await self.store.aclose()

    @property
    def uptime_s(self) -> float:
        return round(time.monotonic() - self.started_at, 1)


def build_runtime(settings: Settings) -> HubRuntime:
    """Wire adapters from settings. Fakes when ``HUB_FAKE_DEVICES=1``, else real adapters."""
    store = StateStore()
    registry = Registry()
    discovery = DiscoveryService(
        settings, registry, search=_noop_search if settings.fake_devices else None
    )
    if settings.fake_devices:
        fakes = FakeBundle(store, tick_interval_s=settings.position_poll_s)
        return HubRuntime(settings, store, list(fakes.adapters), discovery, fakes=fakes)

    from .adapters.denon import HttpDenonAdapter
    from .adapters.heos import PyHeosAdapter
    from .adapters.sonos import SoCoAdapter

    adapters: list[BaseAdapter] = [SoCoAdapter(store, settings)]
    # TODO(Phase 1): hand HEOS/Denon hosts from the discovery registry to the adapters so the
    # env vars become optional overrides.
    heos_host = settings.heos_host or settings.denon_host
    if heos_host:
        adapters.append(PyHeosAdapter(store, settings, heos_host))
    else:
        store.set_connection("heos", "disabled", HEOS_DISABLED_MSG)
    if settings.denon_host:
        adapters.append(HttpDenonAdapter(store, settings, settings.denon_host))
    else:
        store.set_connection("denon", "disabled", DENON_DISABLED_MSG)
    return HubRuntime(settings, store, adapters, discovery)


async def _noop_search() -> list[Any]:
    return []


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
        ],
    )
    app.state.runtime = runtime

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

    return app


def _sorted(items: Sequence[Any] | Any) -> list[Any]:
    return sorted(items, key=lambda i: (getattr(i, "vendor", ""), i.name))
