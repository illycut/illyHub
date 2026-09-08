"""FastAPI application factory.

Phase 0: ``GET /api/health``, ``GET /api/devices``. Phase 1: command endpoints under ``/api``
(transport, seek, skip, volume, mute, zone power, group), the ``/ws`` state stream, and the
fake-only ``/api/dev/fake/{scenario}`` scenario trigger. Phase 2: the art proxy
(``GET /api/art/{key}``) and static serving of the PWA export and fonts. See ``docs/api.md``.
"""

from __future__ import annotations

import asyncio
import socket
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

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
from .auth.tidal import SessionFactory, TidalAuth, default_session_factory
from .auth.vault import Vault, VaultError, open_vault
from .coalesce import Coalescer
from .commands import Ack, CommandError, CommandRouter, ErrorEnvelope, http_status
from .config import Settings, Vendor
from .content import (
    BrowseItem,
    BrowsePage,
    Container,
    ContentNotFoundError,
    ContentRef,
    NeedsLinkError,
)
from .discovery import DiscoveredDevice, DiscoveryService, Registry, SearchFn
from .history import HistoryItem, PlayHistory
from .logsetup import correlation_id, get_logger
from .services.tidal import FakeTidalCatalog, TidalCatalog, TidalService
from .state import ConnectionStatus, Player, Side, StateStore, Zone
from .static import StaticMounts, mount_static
from .tasks import spawn
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
    history: dict[str, Any] = Field(default_factory=dict, description="{ok, last_error}")
    vault: dict[str, Any] = Field(default_factory=dict, description="{ok, last_error}")


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


class PlayBody(BaseModel):
    target: str = Field(description="player id, side id, or 'all'")
    content_ref: ContentRef
    start_index: int = Field(default=0, ge=0)


class LinkStartResponse(BaseModel):
    service: str
    user_code: str
    verification_url: str
    expires_in_s: int
    interval_s: float


AccountState = Literal["linked", "unlinked", "pending", "restoring"]


class AccountStatus(BaseModel):
    service: str
    linked: bool
    state: AccountState = "unlinked"
    account_name: str | None = None
    expires_at: str | None = None
    pending: dict[str, Any] | None = None
    last_error: str | None = None


class HistoryResponse(BaseModel):
    items: list[HistoryItem]


class HomeSection(BaseModel):
    items: list[BrowseItem] = Field(default_factory=list)
    needs_link: str | None = Field(default=None, description="service to link, e.g. 'tidal'")
    error: str | None = Field(default=None, description="section failed; others still render")


class HomeResponse(BaseModel):
    recents: list[HistoryItem]
    playlists: HomeSection
    favorite_albums: HomeSection
    stations: HomeSection


class HubInfo(BaseModel):
    address: str
    port: int
    https: bool
    version: str
    uptime_s: float
    fake_devices: bool


class HardwareItem(BaseModel):
    id: str
    name: str
    vendor: str
    kind: str = Field(description="player | zone")
    model: str | None = None
    ip: str | None = None
    online: bool = True


class SettingsResponse(BaseModel):
    accounts: list[AccountStatus]
    hub: HubInfo
    hardware: list[HardwareItem]


class RefreshResponse(BaseModel):
    cleared: int


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
    vault: Vault | None = None
    tidal_auth: TidalAuth | None = None
    tidal: TidalService | None = None
    fake_tidal: FakeTidalCatalog | None = None
    history: PlayHistory | None = None
    vault_error: str | None = None
    # os._exit is wired only by main.py; anything else (tests, embedding) gets a logged no-op.
    exit_fn: Callable[[int], Any] = lambda code: log.warning(  # noqa: E731
        "restart requested but no exit function is wired", extra={"extra": {"code": code}}
    )
    restarting: bool = False
    started_at: float = field(default_factory=time.monotonic)
    _tasks: set[Any] = field(default_factory=set)

    async def start(self) -> None:
        if self.art is not None:
            await self.art.start()
        if self.history is not None:
            await self.history.open()  # migrate once; failures surface in /api/health
        if self.tidal_auth is not None and self.fake_tidal is None:
            # Token validation is a network call; never block startup on it.
            spawn(self.tidal_auth.restore(), self._tasks)
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
        if self.tidal_auth is not None:
            await self.tidal_auth.aclose()
        from .tasks import cancel_all

        await cancel_all(self._tasks)
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
    tidal_session_factory: SessionFactory | None = None,
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
    vault, vault_error = _open_vault_or_memory(settings)
    tidal_auth = TidalAuth(vault, factory=tidal_session_factory or default_session_factory)
    history = PlayHistory(settings.history_path, max_rows=settings.history_max_rows)
    if settings.fake_devices:
        fakes = FakeBundle(store, tick_interval_s=settings.position_poll_s, art=art)
        router = CommandRouter(
            store,
            playback={"heos": fakes.heos, "sonos": fakes.sonos},
            denon=fakes.denon,
            coalescer=Coalescer(settings.command_coalesce_s),
        )
        fake_catalog: FakeTidalCatalog | None = None
        if settings.fake_tidal:
            fake_catalog = FakeTidalCatalog(art)
            fakes.on_tidal_link = lambda linked: setattr(fake_catalog, "linked", linked)
            tidal = TidalService(
                fake_catalog,
                is_linked=lambda: fake_catalog.linked,
                availability=lambda: router.service_availability("tidal"),
                cache_ttl_s=settings.browse_cache_s,
            )
        else:
            tidal = _real_tidal_service(settings, router, tidal_auth, art)
        return HubRuntime(
            settings,
            store,
            list(fakes.adapters),
            discovery,
            router,
            fakes=fakes,
            art=art_cache,
            vault=vault,
            tidal_auth=tidal_auth,
            tidal=tidal,
            fake_tidal=fake_catalog,
            history=history,
            vault_error=vault_error,
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
        settings,
        store,
        adapters,
        discovery,
        router,
        heos_factory=heos_factory,
        art=art_cache,
        vault=vault,
        tidal_auth=tidal_auth,
        tidal=_real_tidal_service(settings, router, tidal_auth, art),
        history=history,
        vault_error=vault_error,
    )
    if not heos_host:
        discovery.on_new_devices(runtime.adopt_discovered)
    return runtime


def _open_vault_or_memory(settings: Settings) -> tuple[Vault, str | None]:
    """A broken vault (bad HUB_VAULT_KEY, undecryptable file) must not stop the hub: fall back
    to an in-memory vault and surface the error in health and settings."""
    try:
        return open_vault(settings.vault_key, settings.vault_key_path, settings.vault_path), None
    except (VaultError, ValueError, OSError) as exc:
        msg = f"vault unavailable ({exc.__class__.__name__}): {exc}"
        log.error(
            "vault open failed; accounts cannot be persisted", extra={"extra": {"error": msg}}
        )
        return Vault.in_memory(), msg


def _real_tidal_service(
    settings: Settings, router: CommandRouter, auth: TidalAuth, art: ArtHelper
) -> TidalService:
    catalog = TidalCatalog(auth.session_or_raise, art)
    return TidalService(
        catalog,
        is_linked=lambda: auth.linked,
        availability=lambda: router.service_availability("tidal"),
        cache_ttl_s=settings.browse_cache_s,
        before=auth.ensure_fresh,
    )


async def _noop_search() -> list[Any]:
    return []


def lan_address() -> str:
    """Best-effort LAN IP of this machine (the address phones should use)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"


DEFAULT_HOSTS = ("localhost", "127.0.0.1", "*.local")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
RESTART_HEADER = "x-illyhub"


def allowed_hosts(settings: Settings, lan_ip: str | None = None) -> list[str]:
    """Host header allow-list: defaults + the LAN IP + configured names (+ hostnames of configured
    origins). Used by TrustedHostMiddleware and by the Origin check."""
    hosts = [*DEFAULT_HOSTS, lan_ip or lan_address(), *settings.allowed_hosts]
    for origin in settings.allowed_origins:
        host = urlparse(origin).hostname
        if host:
            hosts.append(host)
    seen: set[str] = set()
    return [h for h in hosts if not (h.lower() in seen or seen.add(h.lower()))]


def host_allowed(host: str, patterns: Sequence[str]) -> bool:
    host = host.lower()
    for pattern in patterns:
        pattern = pattern.lower()
        if pattern == host:
            return True
        if pattern.startswith("*.") and (host.endswith(pattern[1:]) or host == pattern[2:]):
            return True
    return False


def origin_allowed(
    origin: str, request_host: str | None, settings: Settings, hosts: Sequence[str]
) -> bool:
    """Same-origin (Origin host:port equals the request's Host) or an origin whose hostname is
    on the host allow-list, or an explicitly configured origin."""
    if origin in settings.allowed_origins:
        return True
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if request_host and parsed.netloc.lower() == request_host.lower():
        return True
    return host_allowed(parsed.hostname, hosts)


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
    exit_fn: Callable[[int], Any] | None = None,
    lan_ip: str | None = None,
) -> FastAPI:
    settings = settings or Settings()
    runtime = runtime_factory(settings)
    if exit_fn is not None:
        runtime.exit_fn = exit_fn
    hosts = allowed_hosts(settings, lan_ip)

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
            {"name": "auth", "description": "Link and unlink streaming accounts"},
            {"name": "browse", "description": "Library browsing through the services' own APIs"},
            {"name": "play", "description": "Play content by canonical id on any target"},
            {"name": "home", "description": "Home screen aggregate and play history"},
            {"name": "settings", "description": "Accounts, hub info, hardware, restart"},
            {"name": "dev", "description": "Fake-device scenario triggers (HUB_FAKE_DEVICES=1)"},
        ],
    )
    app.state.runtime = runtime
    app.state.static_mounted = None  # set once the catch-all is mounted, last
    app.state.allowed_hosts = hosts
    router = runtime.router

    # Unknown Host headers (DNS rebinding) are refused outright.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware("http")
    async def _origin_policy(request: Request, call_next: Callable[[Request], Any]) -> Any:
        """CSRF guard. Bodyless POSTs need no CORS preflight, so a page on any origin could
        otherwise hit restart/unlink/refresh. State-changing requests that carry an Origin must
        come from one of the hub's own origins; requests without Origin (curl, same-origin
        fetches in some browsers) pass. ``POST /api/hub/restart`` additionally requires the
        custom ``X-Illyhub: 1`` header so browsers always preflight it."""
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin and not origin_allowed(origin, request.headers.get("host"), settings, hosts):
                log.warning(
                    "cross-origin request refused",
                    extra={"extra": {"origin": origin, "path": request.url.path}},
                )
                return _envelope(
                    403, "forbidden_origin", "Requests must come from the hub's own origin."
                )
            if (
                request.url.path == "/api/hub/restart"
                and request.headers.get(RESTART_HEADER) != "1"
            ):
                return _envelope(
                    403, "missing_header", "Restart requires the X-Illyhub: 1 request header."
                )
        return await call_next(request)

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

    @app.exception_handler(NeedsLinkError)
    async def _needs_link(_request: Request, exc: NeedsLinkError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"code": "needs_link", "message": exc.message, "service": exc.service},
        )

    @app.exception_handler(ContentNotFoundError)
    async def _content_not_found(_request: Request, exc: ContentNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "code": "not_found",
                "message": exc.message,
                "content_ref": exc.ref.model_dump(),
            },
        )

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
            history=(
                {"ok": runtime.history.ok, "last_error": runtime.history.last_error}
                if runtime.history
                else {}
            ),
            vault={"ok": runtime.vault_error is None, "last_error": runtime.vault_error},
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

    # -- auth (Phase 3) -------------------------------------------------------------

    def _auth() -> TidalAuth:
        assert runtime.tidal_auth is not None
        return runtime.tidal_auth

    def _tidal() -> TidalService:
        assert runtime.tidal is not None
        return runtime.tidal

    def _history() -> PlayHistory:
        assert runtime.history is not None
        return runtime.history

    def _tidal_account() -> AccountStatus:
        if runtime.fake_tidal is not None and runtime.fakes is not None:
            fakes = runtime.fakes
            state = fakes.tidal_state
            return AccountStatus(
                service="tidal",
                linked=fakes.tidal_linked,
                state=state,  # type: ignore[arg-type]
                account_name="fake@tidal" if fakes.tidal_linked else None,
                pending=(
                    {"user_code": "FAKE1", "verification_url": "https://link.tidal.com/FAKE1"}
                    if state == "pending"
                    else None
                ),
                last_error=runtime.vault_error,
            )
        status = AccountStatus(**_auth().status())
        if runtime.vault_error and not status.last_error:
            status.last_error = runtime.vault_error
        return status

    @app.post("/api/auth/tidal/start", response_model=LinkStartResponse, tags=["auth"])
    async def tidal_link_start() -> JSONResponse:
        if runtime.fake_tidal is not None and runtime.fakes is not None:
            runtime.fakes.start_fake_link()
            return JSONResponse(
                content=LinkStartResponse(
                    service="tidal",
                    user_code="FAKE1",
                    verification_url="https://link.tidal.com/FAKE1",
                    expires_in_s=300,
                    interval_s=2.0,
                ).model_dump()
            )
        return JSONResponse(content=(await _auth().start_link()))

    @app.get("/api/auth/tidal/status", response_model=AccountStatus, tags=["auth"])
    async def tidal_link_status() -> AccountStatus:
        return _tidal_account()

    @app.post("/api/auth/tidal/unlink", response_model=AccountStatus, tags=["auth"])
    async def tidal_unlink() -> AccountStatus:
        if runtime.fake_tidal is not None and runtime.fakes is not None:
            runtime.fakes.set_tidal_linked(False)
        else:
            await _auth().unlink()
        _tidal().refresh()
        return _tidal_account()

    # -- browse (Phase 3) -----------------------------------------------------------

    @app.get("/api/browse/tidal/favorites/albums", response_model=BrowsePage, tags=["browse"])
    async def tidal_favorite_albums(limit: int = 50, offset: int = 0) -> BrowsePage:
        return await _tidal().favorite_albums(limit, offset)

    @app.get("/api/browse/tidal/favorites/playlists", response_model=BrowsePage, tags=["browse"])
    async def tidal_favorite_playlists(limit: int = 50, offset: int = 0) -> BrowsePage:
        return await _tidal().favorite_playlists(limit, offset)

    @app.get("/api/browse/tidal/playlists", response_model=BrowsePage, tags=["browse"])
    async def tidal_user_playlists(limit: int = 50, offset: int = 0) -> BrowsePage:
        return await _tidal().user_playlists(limit, offset)

    @app.get("/api/browse/tidal/album/{album_id}", response_model=Container, tags=["browse"])
    async def tidal_album(album_id: str) -> Container:
        return await _tidal().container(ContentRef(service="tidal", kind="album", id=album_id))

    @app.get("/api/browse/tidal/playlist/{playlist_id}", response_model=Container, tags=["browse"])
    async def tidal_playlist(playlist_id: str) -> Container:
        return await _tidal().container(
            ContentRef(service="tidal", kind="playlist", id=playlist_id)
        )

    @app.post("/api/browse/refresh", response_model=RefreshResponse, tags=["browse"])
    async def browse_refresh() -> RefreshResponse:
        return RefreshResponse(cleared=_tidal().refresh())

    # -- play by canonical id (Phase 3) ---------------------------------------------

    @app.post("/api/play", response_model=Ack, tags=["play"])
    async def play_content(body: PlayBody) -> JSONResponse:
        ref = body.content_ref
        if ref.service != "tidal":
            raise NeedsLinkError(
                ref.service, f"{ref.service.title()} playback lands in a later phase."
            )
        item, tracks = await _tidal().tracks_for(ref)
        ack = await router.play_content(
            body.target,
            ref,
            tracks,
            start_index=body.start_index,
            availability=router.service_availability(ref.service),
        )
        if ack.ok:
            await _history().record(
                ref,
                title=item.title,
                subtitle=item.subtitle,
                art=item.art,
                targets=[body.target],
                side_ids=ack.applied,
            )
        return _respond(ack)

    # -- history + home (Phase 3) ----------------------------------------------------

    @app.get("/api/history", response_model=HistoryResponse, tags=["home"])
    async def history(limit: int = 20) -> HistoryResponse:
        return HistoryResponse(items=await _history().recent(limit))

    async def _section(loader: Callable[[], Awaitable[list[BrowseItem]]]) -> HomeSection:
        try:
            return HomeSection(items=await loader())
        except NeedsLinkError as exc:
            return HomeSection(needs_link=exc.service)
        except Exception as exc:  # noqa: BLE001 - one broken section must not sink the home call
            log.exception("home section failed")
            return HomeSection(error=f"{exc.__class__.__name__}: {exc}")

    @app.get("/api/home", response_model=HomeResponse, tags=["home"])
    async def home() -> HomeResponse:
        started = time.perf_counter()
        tidal = _tidal()
        recents = await _history().recent(12)
        last_played = {r.content_ref.key: r.last_played_at for r in recents}

        async def playlists() -> list[BrowseItem]:
            mine = (await tidal.user_playlists(100, 0)).items
            favs = (await tidal.favorite_playlists(100, 0)).items
            seen: set[str] = set()
            merged: list[BrowseItem] = []
            for it in [*mine, *favs]:
                if it.content_ref.key in seen:
                    continue
                seen.add(it.content_ref.key)
                merged.append(it)
            # Recency of play first (hub history), then alphabetical. Names are not deduped
            # across services; the badge disambiguates (PRD §3.4a).
            merged.sort(
                key=lambda it: (
                    0 if it.content_ref.key in last_played else 1,
                    -(last_played[it.content_ref.key].timestamp())
                    if it.content_ref.key in last_played
                    else 0,
                    it.title.casefold(),
                )
            )
            return merged

        async def albums() -> list[BrowseItem]:
            return (await tidal.favorite_albums(100, 0)).items

        response = HomeResponse(
            recents=recents,
            playlists=await _section(playlists),
            favorite_albums=await _section(albums),
            stations=HomeSection(),  # Phase 5
        )
        log.info(
            "home", extra={"extra": {"home_ms": round((time.perf_counter() - started) * 1000, 1)}}
        )
        return response

    # -- settings (Phase 3) -----------------------------------------------------------

    @app.get("/api/settings", response_model=SettingsResponse, tags=["settings"])
    async def settings_view() -> SettingsResponse:
        state = runtime.store.state
        heos_name: str | None = None
        heos_linked = False
        for a in runtime.adapters:
            if a.name == "heos":
                client = getattr(a, "_heos", None)
                heos_linked = bool(getattr(client, "is_signed_in", False)) or bool(runtime.fakes)
                heos_name = getattr(client, "signed_in_username", None) or (
                    "fake@heos" if runtime.fakes else None
                )
        accounts = [
            _tidal_account(),
            AccountStatus(service="ytmusic", linked=False),
            AccountStatus(
                service="pandora",
                linked=router.service_availability("pandora").heos
                or router.service_availability("pandora").sonos,
            ),
            AccountStatus(service="heos_account", linked=heos_linked, account_name=heos_name),
        ]
        hardware = [
            HardwareItem(
                id=p.id,
                name=p.name,
                vendor=p.vendor,
                kind="player",
                model=p.model,
                ip=p.ip,
                online=p.online,
            )
            for p in _sorted(state.players.values())
        ] + [
            HardwareItem(
                id=z.id, name=z.name, vendor="denon", kind="zone", ip=z.host, online=z.online
            )
            for z in _sorted(state.zones.values())
        ]
        return SettingsResponse(
            accounts=accounts,
            hub=HubInfo(
                address=lan_address(),
                port=settings.port,
                https=bool(settings.tls_cert and settings.tls_key),
                version=__version__,
                uptime_s=runtime.uptime_s,
                fake_devices=settings.fake_devices,
            ),
            hardware=hardware,
        )

    @app.post("/api/hub/restart", tags=["settings"], status_code=202)
    async def hub_restart() -> JSONResponse:
        if not settings.allow_restart:
            return _envelope(409, "restart_disabled", "Restart is disabled (HUB_ALLOW_RESTART=0).")
        if runtime.restarting:
            return JSONResponse(status_code=202, content={"restarting": True})
        runtime.restarting = True
        log.warning("restart requested; exiting with code 0 for launchd KeepAlive to relaunch")
        loop = asyncio.get_running_loop()
        loop.call_later(0.5, runtime.exit_fn, 0)
        return JSONResponse(status_code=202, content={"restarting": True})

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
