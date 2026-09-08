"""Hub settings, loaded from environment variables prefixed with ``HUB_`` and from an env file.

The env file defaults to ``.env`` and can be pointed elsewhere with ``HUB_ENV_FILE``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Vendor = Literal["heos", "sonos"]

HUB_ROOT = Path(__file__).resolve().parents[2]  # .../hub


def resolve_path(p: str | Path) -> Path:
    """Relative paths in settings are relative to the ``hub/`` directory, not the CWD, so the
    LaunchAgent and ``uv run hub`` agree."""
    path = Path(p).expanduser()
    return path if path.is_absolute() else (HUB_ROOT / path).resolve()


class StaticDevice(BaseModel):
    """A device pinned by IP so the hub can skip SSDP for it."""

    id: str
    vendor: Vendor
    ip: str
    name: str | None = None
    model: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HUB_", env_file=os.environ.get("HUB_ENV_FILE", ".env"), extra="ignore"
    )

    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    log_file: str | None = None
    stdout_level: str | None = None  # default: INFO without a log file, WARNING with one

    fake_devices: bool = False
    static_devices: list[StaticDevice] = Field(default_factory=list)

    heos_host: str | None = None
    denon_host: str | None = None
    # Telnet is the control path: current firmware 403s every /goform/ endpoint.
    denon_telnet: bool = True
    # Hub volume 100 maps to this MV step, not to MV98 (+18 dB). Raise once trusted.
    denon_max_volume: int = 60
    # Zone keys whose output is a fixed pre-out to an external amp: the receiver cannot
    # attenuate them, so the hub reports them as volume-incapable instead of pretending.
    denon_fixed_zones: list[str] = Field(default_factory=list)
    denon_poll_s: float = 15.0
    denon_zone_fail_threshold: int = 3
    sonos_listener_host: str | None = None

    discovery_interval_s: float = 300.0
    ssdp_timeout_s: float = 3.0
    heos_heartbeat_s: float = 30.0
    reconnect_max_s: float = 60.0
    backoff_reset_after_s: float = 30.0
    position_poll_s: float = 1.0
    command_coalesce_s: float = 0.1  # min gap between device writes per volume/seek target

    # Phase 2: art proxy, static PWA serving, TLS
    data_dir: str = "data"  # relative to hub/; gitignored
    art_proxy: bool = True
    art_ttl_days: float = 30.0
    art_max_mb: float = 500.0  # LRU trim budget for the variants on disk
    art_fetch_timeout_s: float = 5.0
    app_dir: str = "../app/out"  # Next.js static export, relative to hub/
    fonts_dir: str = "../app/public/fonts"  # self-hosted General Sans, relative to hub/
    tls_cert: str | None = None  # PEM cert; with tls_key uvicorn serves HTTPS (mkcert path)
    tls_key: str | None = None

    # Phase 3: auth vault, Tidal, play history, settings
    vault_key: str | None = None  # Fernet key; generated into $HUB_DATA_DIR/vault.key when unset
    fake_tidal: bool = False  # canned Tidal library + fake queue (needs HUB_FAKE_DEVICES=1)
    sonos_tidal_sn: str | None = None  # Sonos account serial for Tidal URIs when discovery fails
    # Phase 5: Pandora stations, played natively per ecosystem (PRD §3.5)
    fake_pandora: bool = False  # canned stations on the fakes (needs HUB_FAKE_DEVICES=1)
    sonos_pandora_sn: str | None = None  # Sonos account serial for Pandora URIs (manual)
    # Phase 6: YouTube Music (browse via ytmusicapi with a Google TV-type OAuth client; native
    # playback on Sonos; HEOS has no YouTube Music source — docs/spikes/ytmusic-heos.md)
    ytmusic_client_id: str | None = None
    ytmusic_client_secret: str | None = None
    fake_ytmusic: bool = False  # canned YouTube Music library (needs HUB_FAKE_DEVICES=1)
    sonos_ytmusic_sn: str | None = None  # Sonos account serial for YouTube Music (type 72711)
    sonos_ytmusic_uri: str | None = None  # override the assumed track URI template (LAN run)
    ytmusic_max_tracks: int = 500  # cap per album/playlist fetch (Container.truncated when hit)
    ytmusic_max_library_items: int = 1000  # cap on library playlists/albums paged from YouTube
    browse_cache_s: float = 300.0
    history_max_rows: int = 500
    allow_restart: bool = True
    # Phase 8 (ai-dev #72): self-update via ops/update.sh. HUB_REPO_DIR defaults to the checkout
    # the hub runs from (inferred from this package's location).
    # DECISION (Phase 8 review): off by default. The endpoint runs git and the dependency sync as
    # root on the hub Mac, gated only by the LAN; ops/install.sh asks the operator and records
    # the answer in hub/.env.
    allow_update: bool = False
    repo_dir: str | None = None
    update_branch: str = "main"  # followed when the checkout's branch has no upstream
    # Phase 8 (ai-dev #77 / #78): native queue read cap, per-service search deadline and cache.
    queue_max: int = 200
    search_deadline_s: float = 4.0
    search_cache_s: float = 60.0
    metrics_tick_s: float = 60.0  # uptime tick / daily summary cadence (ai-dev #73)
    # Request-origin policy. Bodyless cross-origin POSTs need no CORS preflight, so the hub
    # rejects state-changing requests whose Origin is not one of its own, and refuses unknown
    # Host headers (DNS rebinding). Add tailnet / mkcert names here.
    allowed_hosts: list[str] = Field(default_factory=list)  # extra hosts beyond the defaults
    allowed_origins: list[str] = Field(default_factory=list)  # extra full origins

    # Phase 4: Sync Play (docs/sync-engine.md). Hot-reloadable via POST /api/sync/config.
    sync_drift_ms: int = 300
    sync_samples: int = 2
    sync_correction_gap_s: float = 10.0
    sync_lookahead_ms: int = 400
    sync_monitor_s: float = 0.5
    sync_track_grace_s: float = 3.0
    sync_max_sessions: int = 50  # drift logs kept on disk

    # Phase 7 (P2, experimental): the hub Mac as an AirPlay 2 sender for "Pandora Sync"
    # (PRD §3.5 PAN-4, docs/spikes/airplay-bridge.md). Off by default: it drives the Music app
    # over AppleScript and needs a logged-in GUI session plus Automation permission.
    airplay_enabled: bool = False
    airplay_timeout_s: float = 20.0  # osascript budget; Music's cold launch can take ~30 s
    airplay_launch_timeout_s: float = 60.0  # first call after Music was closed

    @property
    def data_path(self) -> Path:
        return resolve_path(self.data_dir)

    @property
    def art_path(self) -> Path:
        return self.data_path / "art"

    @property
    def vault_path(self) -> Path:
        return self.data_path / "vault.json"

    @property
    def vault_key_path(self) -> Path:
        return self.data_path / "vault.key"

    @property
    def history_path(self) -> Path:
        return self.data_path / "history.sqlite"

    @property
    def sync_log_path(self) -> Path:
        return self.data_path / "sync"

    @property
    def metrics_path(self) -> Path:
        return self.data_path / "metrics.sqlite"

    @property
    def repo_path(self) -> Path:
        if self.repo_dir:
            return resolve_path(self.repo_dir)
        return HUB_ROOT.parent

    @property
    def app_path(self) -> Path:
        return resolve_path(self.app_dir)

    @property
    def fonts_path(self) -> Path:
        return resolve_path(self.fonts_dir)

    @property
    def tls_cert_path(self) -> Path | None:
        return resolve_path(self.tls_cert) if self.tls_cert else None

    @property
    def tls_key_path(self) -> Path | None:
        return resolve_path(self.tls_key) if self.tls_key else None

    @property
    def effective_stdout_level(self) -> str:
        if self.stdout_level:
            return self.stdout_level
        return "WARNING" if self.log_file else self.log_level
