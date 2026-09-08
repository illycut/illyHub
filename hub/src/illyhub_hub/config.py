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
    denon_telnet: bool = False
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

    @property
    def data_path(self) -> Path:
        return resolve_path(self.data_dir)

    @property
    def art_path(self) -> Path:
        return self.data_path / "art"

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
