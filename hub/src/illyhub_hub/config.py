"""Hub settings, loaded from environment variables prefixed with ``HUB_`` and from an env file.

The env file defaults to ``.env`` and can be pointed elsewhere with ``HUB_ENV_FILE``.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Vendor = Literal["heos", "sonos"]


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

    @property
    def effective_stdout_level(self) -> str:
        if self.stdout_level:
            return self.stdout_level
        return "WARNING" if self.log_file else self.log_level
