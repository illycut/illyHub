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
from .airplay import AirPlayOutput, PandoraSyncController, build_bridge
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
from .auth.ytmusic import ClientFactory, CredentialsFactory, YTMusicAuth
from .auth.ytmusic import default_client_factory as default_ytmusic_client_factory
from .auth.ytmusic import default_credentials_factory as default_ytmusic_credentials_factory
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
from .messages import export as messages_export
from .services.pandora import PAGE_MAX as STATIONS_PAGE_MAX
from .services.pandora import PandoraService
from .services.tidal import FakeTidalCatalog, TidalCatalog, TidalService
from .services.ytmusic import FakeYTMusicCatalog, YTMusicCatalog
from .state import (
    ConnectionStatus,
    PandoraSyncState,
    Player,
    Side,
    StateStore,
    SyncState,
    Zone,
    service_label,
)
from .static import StaticMounts, mount_static
from .sync import SyncConfig, SyncEngine, SyncReport, SyncSessionSummary
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
    # Machine-readable companion to last_error: "needs_client_config" when the hub owner must add
    # OAuth client credentials (the app renders "Hub setup needed" and keeps the env-var text for
    # the owner's sheet).
    last_error_code: str | None = None
    # Per-ecosystem services (Pandora): true = browse succeeded there, false = not linked /
    # auth fault, null = vendor absent or its browse errored / not asked yet.
    linked_by_vendor: dict[str, bool | None] | None = None


class HistoryResponse(BaseModel):
    items: list[HistoryItem]


class HomeSection(BaseModel):
    items: list[BrowseItem] = Field(default_factory=list)
    needs_link: list[str] = Field(
        default_factory=list,
        description=(
            "hub-linked services that contribute to this section and are currently unlinked "
            "(e.g. ['tidal']); items from every linked service still render"
        ),
    )
    error: str | None = Field(default=None, description="section failed; others still render")
    linked: dict[str, bool | None] | None = Field(
        default=None,
        description=(
            "per-source linkage: library sections map service -> true | false | null (browse "
            "errored); the stations section maps vendor -> true | false | null (unknown)"
        ),
    )


class ErrorBody(BaseModel):
    """Bare error envelope shape (non-command routes) for OpenAPI ``responses``."""

    code: str
    message: str


class HomeResponse(BaseModel):
    recents: list[HistoryItem]
    playlists: HomeSection
    favorite_albums: HomeSection
    stations: HomeSection


class AirPlayInfo(BaseModel):
    """Experimental AirPlay bridge status (PRD §3.5 PAN-4). ``enabled`` follows
    ``HUB_AIRPLAY_ENABLED`` (always true in fake mode, which carries a fake bridge);
    ``available`` is whether Music can be scripted from this process right now."""

    enabled: bool
    available: bool
    reason: str | None = None


class HubInfo(BaseModel):
    address: str
    port: int
    https: bool
    version: str
    uptime_s: float
    fake_devices: bool
    airplay: AirPlayInfo


class AirPlayStatusResponse(BaseModel):
    enabled: bool
    available: bool
    reason: str | None = None
    outputs: list[AirPlayOutput] = Field(default_factory=list)


class AirPlayOutputsBody(BaseModel):
    ids: list[str] = Field(
        min_length=1, max_length=32, description="AirPlay output ids to select (others off)"
    )


class PandoraSyncStartBody(BaseModel):
    """Exactly one of ``side_ids`` (rooms; the hub matches their names to outputs) or
    ``output_ids`` (explicit AirPlay outputs)."""

    side_ids: list[str] | None = Field(default=None, min_length=1, max_length=32)
    output_ids: list[str] | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def _one_of(self) -> PandoraSyncStartBody:
        if (self.side_ids is None) == (self.output_ids is None):
            raise ValueError("give exactly one of side_ids or output_ids")
        return self


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


SyncTarget = str | list[str]


class SyncPlayBody(BaseModel):
    content_ref: ContentRef
    heos_target: SyncTarget = Field(
        description="HEOS player id, side id, or a list of player ids (grouped first)"
    )
    sonos_target: SyncTarget = Field(
        description="Sonos player id, side id, or a list of player ids (grouped first)"
    )
    start_index: int = Field(default=0, ge=0)


class SyncConfigBody(BaseModel):
    drift_ms: int | None = Field(default=None, ge=50, le=5000)
    samples: int | None = Field(default=None, ge=1, le=20)
    correction_gap_s: float | None = Field(default=None, ge=0.1, le=120.0)
    lookahead_ms: int | None = Field(default=None, ge=0, le=5000)
    monitor_s: float | None = Field(default=None, ge=0.01, le=5.0)
    track_grace_s: float | None = Field(default=None, ge=0.1, le=30.0)


class SyncSessionsResponse(BaseModel):
    sessions: list[SyncSessionSummary]


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
    ytmusic_auth: YTMusicAuth | None = None
    ytmusic: TidalService | None = None  # same facade, service="ytmusic"
    fake_ytmusic: FakeYTMusicCatalog | None = None
    pandora: PandoraService | None = None
    history: PlayHistory | None = None
    sync: SyncEngine | None = None
    airplay: PandoraSyncController | None = None
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
        if self.ytmusic_auth is not None and self.fake_ytmusic is None:
            spawn(self.ytmusic_auth.restore(), self._tasks)
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
        if self.sync is not None:
            await self.sync.aclose()
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
        if self.ytmusic_auth is not None:
            await self.ytmusic_auth.aclose()
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
    ytmusic_credentials_factory: CredentialsFactory | None = None,
    ytmusic_client_factory: ClientFactory | None = None,
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
    ytmusic_auth = YTMusicAuth(
        vault,
        client_id=settings.ytmusic_client_id,
        client_secret=settings.ytmusic_client_secret,
        credentials_factory=ytmusic_credentials_factory or default_ytmusic_credentials_factory,
        client_factory=ytmusic_client_factory or default_ytmusic_client_factory,
    )
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
        fake_yt: FakeYTMusicCatalog | None = None
        if settings.fake_ytmusic:
            fake_yt = FakeYTMusicCatalog(art)
            fakes.on_ytmusic_link = lambda linked: setattr(fake_yt, "linked", linked)
            ytmusic = TidalService(
                fake_yt,
                is_linked=lambda: fake_yt.linked,
                availability=lambda: router.service_availability("ytmusic"),
                cache_ttl_s=settings.browse_cache_s,
                service="ytmusic",
            )
        else:
            fakes.set_ytmusic_linked(False)  # no canned library: Sonos side has no account
            ytmusic = _real_ytmusic_service(settings, router, ytmusic_auth, art)
        fakes.pandora_enabled = settings.fake_pandora
        pandora = _pandora_service(settings, router, art)
        fakes.on_pandora_link = lambda: pandora.refresh()  # link state changed: cache is stale
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
            ytmusic_auth=ytmusic_auth,
            ytmusic=ytmusic,
            fake_ytmusic=fake_yt,
            pandora=pandora,
            history=history,
            sync=_sync_engine(settings, store, router, tidal, history),
            airplay=PandoraSyncController(store, router, build_bridge(settings), enabled=True),
            vault_error=vault_error,
        )

    from .adapters.denon import DenonControlAdapter
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
        denon = DenonControlAdapter(store, settings, settings.denon_host)
        adapters.append(denon)
        router.register(denon)
    else:
        store.set_connection("denon", "disabled", DENON_DISABLED_MSG)
    tidal = _real_tidal_service(settings, router, tidal_auth, art)
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
        tidal=tidal,
        ytmusic_auth=ytmusic_auth,
        ytmusic=_real_ytmusic_service(settings, router, ytmusic_auth, art),
        pandora=_pandora_service(settings, router, art),
        history=history,
        sync=_sync_engine(settings, store, router, tidal, history),
        airplay=PandoraSyncController(
            store, router, build_bridge(settings), enabled=settings.airplay_enabled
        ),
        vault_error=vault_error,
    )
    if not heos_host:
        discovery.on_new_devices(runtime.adopt_discovered)
    return runtime


def _sync_engine(
    settings: Settings,
    store: StateStore,
    router: CommandRouter,
    tidal: TidalService,
    history: PlayHistory,
) -> SyncEngine:
    return SyncEngine(
        store,
        router,
        tracks_for=tidal.tracks_for,
        history=history,
        config=SyncConfig(
            drift_ms=settings.sync_drift_ms,
            samples=settings.sync_samples,
            correction_gap_s=settings.sync_correction_gap_s,
            lookahead_ms=settings.sync_lookahead_ms,
            monitor_s=settings.sync_monitor_s,
            track_grace_s=settings.sync_track_grace_s,
        ),
        log_dir=settings.sync_log_path,
        max_sessions=settings.sync_max_sessions,
    )


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


def _pandora_service(settings: Settings, router: CommandRouter, art: ArtHelper) -> PandoraService:
    """Stations are browsed per ecosystem through the adapters the router knows about (the HEOS
    adapter may be adopted later from discovery, hence the callable)."""
    return PandoraService(
        lambda: router.playback,
        art=art,
        is_connected=lambda vendor: (
            (a := router.playback.get(vendor)) is not None and a.status().state == "connected"
        ),
        cache_ttl_s=settings.browse_cache_s,
    )


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


def _real_ytmusic_service(
    settings: Settings, router: CommandRouter, auth: YTMusicAuth, art: ArtHelper
) -> TidalService:
    catalog = YTMusicCatalog(
        auth.client_or_raise,
        art,
        max_tracks=settings.ytmusic_max_tracks,
        max_library_items=settings.ytmusic_max_library_items,
    )
    return TidalService(
        catalog,
        is_linked=lambda: auth.linked,
        availability=lambda: router.service_availability("ytmusic"),
        cache_ttl_s=settings.browse_cache_s,
        before=auth.ensure_fresh,
        service="ytmusic",
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
# POSTs that act on the hub Mac itself (restart, AirPlay routing, opening a browser) must always
# preflight in a browser, so they require the custom header even from an allowed origin.
HEADER_REQUIRED_PATHS = frozenset(
    {
        "/api/hub/restart",
        "/api/airplay/outputs",
        "/api/pandora-sync/start",
        "/api/pandora-sync/stop",
    }
)


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
            {"name": "sync", "description": "Sync Play: HEOS master + Sonos follower (Phase 4)"},
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
                request.url.path in HEADER_REQUIRED_PATHS
                and request.headers.get(RESTART_HEADER) != "1"
            ):
                return _envelope(
                    403,
                    "missing_header",
                    "This request requires the X-Illyhub: 1 request header.",
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
            content={"code": exc.code, "message": exc.message, "service": exc.service},
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

    # -- copy bundle for the app (Phase 6) ----------------------------------------------

    @app.get("/api/meta/messages", tags=["meta"])
    async def meta_messages() -> dict[str, Any]:
        """Labels, unsupported pairs and message templates the app's copy tests read."""
        return messages_export()

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

    def _pandora() -> PandoraService:
        assert runtime.pandora is not None
        return runtime.pandora

    def _yt_auth() -> YTMusicAuth:
        assert runtime.ytmusic_auth is not None
        return runtime.ytmusic_auth

    def _ytmusic() -> TidalService:
        assert runtime.ytmusic is not None
        return runtime.ytmusic

    def _ytmusic_account() -> AccountStatus:
        if runtime.fake_ytmusic is not None and runtime.fakes is not None:
            fakes = runtime.fakes
            state = fakes.ytmusic_state
            return AccountStatus(
                service="ytmusic",
                linked=fakes.ytmusic_linked,
                state=state,  # type: ignore[arg-type]
                account_name="fake@ytmusic" if fakes.ytmusic_linked else None,
                pending=(
                    {"user_code": "FAKE-YT-1", "verification_url": "https://www.google.com/device"}
                    if state == "pending"
                    else None
                ),
                last_error=runtime.vault_error,
            )
        status = AccountStatus(**_yt_auth().status())
        if runtime.vault_error and not status.last_error:
            status.last_error = runtime.vault_error
        return status

    def _pandora_account() -> AccountStatus:
        """Pandora is linked per ecosystem; ``linked`` is "either vendor reports an account",
        ``linked_by_vendor`` the per-side truth from the last browse, ``last_error`` an auth fault
        or browse error the settings row should show."""
        avail = router.service_availability("pandora")
        linked = avail.heos or avail.sonos
        return AccountStatus(
            service="pandora",
            linked=linked,
            state="linked" if linked else "unlinked",
            linked_by_vendor=_pandora().linked,
            last_error=_pandora().last_error,
        )

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

    # -- YouTube Music (Phase 6) ---------------------------------------------------------

    @app.post("/api/auth/ytmusic/start", response_model=LinkStartResponse, tags=["auth"])
    async def ytmusic_link_start() -> JSONResponse:
        if runtime.fake_ytmusic is not None and runtime.fakes is not None:
            runtime.fakes.start_fake_ytmusic_link()
            return JSONResponse(
                content=LinkStartResponse(
                    service="ytmusic",
                    user_code="FAKE-YT-1",
                    verification_url="https://www.google.com/device",
                    expires_in_s=1800,
                    interval_s=5.0,
                ).model_dump()
            )
        return JSONResponse(content=(await _yt_auth().start_link()))

    @app.get("/api/auth/ytmusic/status", response_model=AccountStatus, tags=["auth"])
    async def ytmusic_link_status() -> AccountStatus:
        return _ytmusic_account()

    @app.post("/api/auth/ytmusic/unlink", response_model=AccountStatus, tags=["auth"])
    async def ytmusic_unlink() -> AccountStatus:
        if runtime.fake_ytmusic is not None and runtime.fakes is not None:
            runtime.fakes.set_ytmusic_linked(False)
        else:
            await _yt_auth().unlink()
        _ytmusic().refresh()
        return _ytmusic_account()

    @app.get("/api/browse/ytmusic/playlists", response_model=BrowsePage, tags=["browse"])
    async def ytmusic_playlists(limit: int = 50, offset: int = 0) -> BrowsePage:
        """The user's library playlists (owned and saved; ytmusicapi does not separate them)."""
        return await _ytmusic().user_playlists(limit, offset)

    @app.get("/api/browse/ytmusic/albums", response_model=BrowsePage, tags=["browse"])
    async def ytmusic_albums(limit: int = 50, offset: int = 0) -> BrowsePage:
        """Library (liked) albums."""
        return await _ytmusic().favorite_albums(limit, offset)

    @app.get("/api/browse/ytmusic/album/{album_id}", response_model=Container, tags=["browse"])
    async def ytmusic_album(album_id: str) -> Container:
        return await _ytmusic().container(ContentRef(service="ytmusic", kind="album", id=album_id))

    @app.get(
        "/api/browse/ytmusic/playlist/{playlist_id}", response_model=Container, tags=["browse"]
    )
    async def ytmusic_playlist(playlist_id: str) -> Container:
        return await _ytmusic().container(
            ContentRef(service="ytmusic", kind="playlist", id=playlist_id)
        )

    @app.get(
        "/api/stream/ytmusic/{video_id}",
        tags=["browse"],
        responses={501: {"model": ErrorBody, "description": "HEOS bridge not implemented"}},
    )
    async def ytmusic_stream(video_id: str) -> JSONResponse:
        """Reserved for the HEOS yt-dlp bridge (ai-dev #67). The spike verdict is no-go for now:
        docs/spikes/ytmusic-heos.md. HEOS sides report YouTube Music as unavailable."""
        return _envelope(
            501,
            "not_implemented",
            "YouTube Music on HEOS is not implemented; see docs/spikes/ytmusic-heos.md.",
        )

    @app.get("/api/browse/pandora/stations", response_model=BrowsePage, tags=["browse"])
    async def pandora_stations() -> BrowsePage:
        """Merged station list (PAN-3): one item per station name, ``availability`` per side."""
        pandora = _pandora()
        items = await pandora.stations()
        return BrowsePage(
            items=items,
            offset=0,
            limit=STATIONS_PAGE_MAX,
            total=len(items),
            linked=pandora.linked,
        )

    @app.post("/api/browse/refresh", response_model=RefreshResponse, tags=["browse"])
    async def browse_refresh() -> RefreshResponse:
        return RefreshResponse(
            cleared=_tidal().refresh() + _ytmusic().refresh() + _pandora().refresh()
        )

    # -- play by canonical id (Phase 3) ---------------------------------------------

    @app.post("/api/play", response_model=Ack, tags=["play"])
    async def play_content(body: PlayBody) -> JSONResponse:
        ref = body.content_ref
        if ref.service == "pandora":
            if ref.kind != "station":
                raise ContentNotFoundError(ref)
            station = await _pandora().station(ref)
            # Vendor-level availability (is Pandora linked in that app) is the router's; the
            # station's own per-vendor presence is judged from ``vendor_stations``.
            ack = await router.play_station(
                body.target,
                ref,
                _pandora().vendor_stations(ref),
                availability=router.service_availability(ref.service),
                auth_faults=_pandora().auth_faults,
            )
            if ack.ok:
                await _history().record(
                    ref,
                    title=station.title,
                    subtitle=station.subtitle,
                    art=station.art,
                    targets=[body.target],
                    side_ids=ack.applied,
                    availability=station.availability,
                )
            return _respond(ack)
        if ref.service == "tidal":
            library = _tidal()
        elif ref.service == "ytmusic":
            library = _ytmusic()
        else:
            raise NeedsLinkError(
                ref.service, f"{service_label(ref.service)} playback lands in a later phase."
            )
        item, tracks = await library.tracks_for(ref)
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
                availability=item.availability,
            )
        return _respond(ack)

    # -- Sync Play (Phase 4) -----------------------------------------------------------

    def _sync() -> SyncEngine:
        assert runtime.sync is not None
        return runtime.sync

    @app.post("/api/sync/play", response_model=Ack, tags=["sync"])
    async def sync_play(body: SyncPlayBody) -> JSONResponse:
        ack = await _sync().play(
            body.content_ref, body.heos_target, body.sonos_target, body.start_index
        )
        return _respond(ack)

    @app.post("/api/sync/stop", response_model=Ack, tags=["sync"])
    async def sync_stop() -> JSONResponse:
        return _respond(await _sync().stop())

    @app.post("/api/sync/retry", response_model=Ack, tags=["sync"])
    async def sync_retry() -> JSONResponse:
        return _respond(await _sync().retry())

    @app.get("/api/sync", response_model=SyncState, tags=["sync"])
    async def sync_state() -> SyncState:
        return _sync().current()

    @app.get("/api/sync/sessions", response_model=SyncSessionsResponse, tags=["sync"])
    async def sync_sessions(limit: int = 50) -> SyncSessionsResponse:
        return SyncSessionsResponse(sessions=await _sync().sessions(max(1, min(limit, 500))))

    @app.get("/api/sync/sessions/{session_id}/report", response_model=SyncReport, tags=["sync"])
    async def sync_report(session_id: str) -> Any:
        report = await _sync().report(session_id)
        if report is None:
            return _envelope(404, "not_found", f"No Sync Play session {session_id!r}.")
        return report

    @app.get("/api/sync/config", response_model=SyncConfig, tags=["sync"])
    async def sync_config() -> SyncConfig:
        return _sync().config

    @app.post("/api/sync/config", response_model=SyncConfig, tags=["sync"])
    async def sync_config_update(body: SyncConfigBody) -> SyncConfig:
        changes = {k: v for k, v in body.model_dump().items() if v is not None}
        return await _sync().update_config(**changes)

    # -- history + home (Phase 3) ----------------------------------------------------

    @app.get("/api/history", response_model=HistoryResponse, tags=["home"])
    async def history(limit: int = 20) -> HistoryResponse:
        return HistoryResponse(items=_with_live_availability(await _history().recent(limit)))

    def _with_live_availability(items: list[HistoryItem]) -> list[HistoryItem]:
        """History rows carry the availability snapshotted at play time; when served, refresh it
        from what the hub knows now (Tidal: which vendor apps have Tidal linked; stations: the
        current merged station map), so the recents rail greys a vendor that lost the item."""
        tidal_avail = router.service_availability("tidal")
        yt_avail = router.service_availability("ytmusic")
        for it in items:
            ref = it.content_ref
            if ref.service == "tidal":
                it.availability = tidal_avail
            elif ref.service == "ytmusic":
                it.availability = yt_avail
            elif ref.service == "pandora" and ref.kind == "station":
                by_vendor = _pandora().vendor_stations(ref)
                if by_vendor:
                    it.availability = _pandora().station_availability_for(by_vendor)
        return items

    async def _section(loader: Callable[[], Awaitable[list[BrowseItem]]]) -> HomeSection:
        try:
            return HomeSection(items=await loader())
        except NeedsLinkError as exc:
            return HomeSection(needs_link=[exc.service])
        except Exception as exc:  # noqa: BLE001 - one broken section must not sink the home call
            log.exception("home section failed")
            return HomeSection(error=f"{exc.__class__.__name__}: {exc}")

    async def _library_section(
        sources: list[tuple[str, TidalService, Callable[[], Awaitable[list[BrowseItem]]]]],
        last_played: dict[str, Any],
    ) -> HomeSection:
        """Merge one home section from several hub-linked library services (Tidal, YouTube
        Music). Every service is guarded on its own: an unlinked one lands in ``needs_link``, a
        failing one lands as ``linked[service] = null`` with the error, and items from every
        working service always render — a single-service household is a launch configuration."""
        items: list[BrowseItem] = []
        linked: dict[str, bool | None] = {}
        needs_link: list[str] = []
        errors: list[str] = []
        for name, svc, loader in sources:
            if not svc.linked:
                linked[name] = False
                needs_link.append(name)
                continue
            try:
                items.extend(await loader())
                linked[name] = True
            except NeedsLinkError:
                linked[name] = False
                needs_link.append(name)
            except Exception as exc:  # noqa: BLE001 - one service must not sink the section
                log.warning(
                    "home: library source failed",
                    extra={"extra": {"service": name, "error": str(exc)}},
                )
                linked[name] = None
                errors.append(f"{service_label(name)}: {exc.__class__.__name__}: {exc}")
        seen: set[str] = set()
        merged: list[BrowseItem] = []
        for it in items:
            if it.content_ref.key in seen:
                continue
            seen.add(it.content_ref.key)
            merged.append(it)
        # Recency of play first (hub history), then alphabetical. Names are not deduped across
        # services; the badge disambiguates (PRD §3.4a).
        merged.sort(
            key=lambda it: (
                0 if it.content_ref.key in last_played else 1,
                -(last_played[it.content_ref.key].timestamp())
                if it.content_ref.key in last_played
                else 0,
                it.title.casefold(),
            )
        )
        return HomeSection(
            items=merged,
            needs_link=needs_link,
            linked=linked,
            error="; ".join(errors) if errors and not merged else None,
        )

    async def _stations_section() -> HomeSection:
        section = await _section(_pandora().stations)
        section.linked = _pandora().linked
        return section

    @app.get("/api/home", response_model=HomeResponse, tags=["home"])
    async def home() -> HomeResponse:
        started = time.perf_counter()
        tidal = _tidal()
        recents = await _history().recent(12)
        last_played = {r.content_ref.key: r.last_played_at for r in recents}
        stations_section = await _stations_section()  # first: warms the station map for recents
        recents = _with_live_availability(recents)

        ytmusic = _ytmusic()

        async def tidal_playlists() -> list[BrowseItem]:
            mine = (await tidal.user_playlists(100, 0)).items
            favs = (await tidal.favorite_playlists(100, 0)).items
            return [*mine, *favs]

        async def yt_playlists() -> list[BrowseItem]:
            return (await ytmusic.user_playlists(100, 0)).items

        async def tidal_albums() -> list[BrowseItem]:
            return (await tidal.favorite_albums(100, 0)).items

        async def yt_albums() -> list[BrowseItem]:
            return (await ytmusic.favorite_albums(100, 0)).items

        playlists_section = await _library_section(
            [("tidal", tidal, tidal_playlists), ("ytmusic", ytmusic, yt_playlists)], last_played
        )
        albums_section = await _library_section(
            [("tidal", tidal, tidal_albums), ("ytmusic", ytmusic, yt_albums)], last_played
        )

        response = HomeResponse(
            recents=recents,
            playlists=playlists_section,
            favorite_albums=albums_section,
            stations=stations_section,
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
            _ytmusic_account(),
            _pandora_account(),
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
        ap_enabled, ap_available, ap_reason = (
            await runtime.airplay.info() if runtime.airplay else (False, False, None)
        )
        return SettingsResponse(
            accounts=accounts,
            hub=HubInfo(
                address=lan_address(),
                port=settings.port,
                https=bool(settings.tls_cert and settings.tls_key),
                version=__version__,
                uptime_s=runtime.uptime_s,
                fake_devices=settings.fake_devices,
                airplay=AirPlayInfo(enabled=ap_enabled, available=ap_available, reason=ap_reason),
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

    # -- AirPlay bridge / Pandora Sync (Phase 7, experimental) ----------------------

    def _airplay() -> PandoraSyncController:
        if runtime.airplay is None:  # pragma: no cover - build_runtime always wires it
            raise RuntimeError("airplay controller not wired")
        return runtime.airplay

    @app.get("/api/airplay", response_model=AirPlayStatusResponse, tags=["airplay"])
    async def airplay_status() -> AirPlayStatusResponse:
        enabled, available, reason = await _airplay().info()
        outputs: list[AirPlayOutput] = []
        if available:
            try:
                outputs = await _airplay().outputs()
            except CommandError as exc:
                available, reason = False, exc.message
        return AirPlayStatusResponse(
            enabled=enabled, available=available, reason=reason, outputs=outputs
        )

    @app.post("/api/airplay/outputs", response_model=Ack, tags=["airplay"])
    async def airplay_outputs(body: AirPlayOutputsBody) -> JSONResponse:
        return _respond(await _airplay().set_outputs(body.ids))

    @app.get("/api/pandora-sync", response_model=PandoraSyncState, tags=["airplay"])
    async def pandora_sync_state() -> PandoraSyncState:
        return runtime.store.state.pandora_sync

    @app.post("/api/pandora-sync/start", response_model=Ack, tags=["airplay"])
    async def pandora_sync_start(body: PandoraSyncStartBody) -> JSONResponse:
        return _respond(await _airplay().start(side_ids=body.side_ids, output_ids=body.output_ids))

    @app.post("/api/pandora-sync/stop", response_model=Ack, tags=["airplay"])
    async def pandora_sync_stop() -> JSONResponse:
        return _respond(await _airplay().stop())

    # -- fake scenarios -------------------------------------------------------------

    if runtime.fakes is not None:
        fakes = runtime.fakes

        @app.post("/api/dev/fake/{scenario}", response_model=ScenarioResponse, tags=["dev"])
        async def fake_scenario(scenario: str, vendor: str | None = None) -> JSONResponse:
            try:
                result = fakes.run_scenario(scenario, vendor=vendor)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={
                        "code": "invalid_argument",
                        "message": f"vendor must be heos or sonos, not {vendor!r}.",
                    },
                )
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
