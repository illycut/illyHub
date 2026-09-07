"""Device discovery: SSDP M-SEARCH for HEOS and Sonos plus a static list that bypasses SSDP.

Both paths produce :class:`DiscoveredDevice` entries in one :class:`Registry` keyed by a
stable id (Sonos ``RINCON_...`` uid, HEOS device uuid, or the configured static id).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import socket
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .config import Settings, StaticDevice, Vendor
from .logsetup import get_logger
from .state import now

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH_TARGETS: dict[str, Vendor] = {
    "urn:schemas-denon-com:device:ACT-Denon:1": "heos",
    "urn:schemas-upnp-org:device:ZonePlayer:1": "sonos",
}
MSEARCH_REPEAT = 3
MSEARCH_STAGGER_S = 0.25
_HOST_RE = re.compile(r"^https?://([^/:]+)(?::(\d+))?")
_UUID_RE = re.compile(r"uuid:([^:]+)")
_LINE_RE = re.compile(r"\r?\n")

log = get_logger("discovery")


class DiscoveredDevice(BaseModel):
    id: str
    vendor: Vendor
    ip: str
    name: str | None = None
    model: str | None = None
    location: str | None = None
    source: str = "ssdp"
    last_seen: datetime = Field(default_factory=now)


def build_msearch(st: str, mx: int = 2) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {mx}\r\n"
        f"ST: {st}\r\n\r\n"
    ).encode()


def parse_ssdp_response(data: bytes, addr: tuple[str, int]) -> DiscoveredDevice | None:
    """Turn one SSDP response datagram into a device, or None if it is not one we want."""
    text = data.decode(errors="ignore")
    headers: dict[str, str] = {}
    for line in _LINE_RE.split(text)[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().upper()] = v.strip()
    vendor = SEARCH_TARGETS.get(headers.get("ST", ""))
    if vendor is None:
        return None
    location = headers.get("LOCATION")
    host_match = _HOST_RE.match(location or "")
    ip = host_match.group(1) if host_match else addr[0]
    port = host_match.group(2) if host_match and host_match.group(2) else str(addr[1])
    match = _UUID_RE.search(headers.get("USN", ""))
    if match:
        device_id = match.group(1)
    else:
        # No USN: derive a stable id from the location so two devices on one host do not collide.
        digest = hashlib.sha1((location or f"{ip}:{port}").encode()).hexdigest()[:8]
        device_id = f"{vendor}-{ip}-{port}-{digest}"
    return DiscoveredDevice(id=device_id, vendor=vendor, ip=ip, location=location)


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: dict[str, DiscoveredDevice] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        device = parse_ssdp_response(data, addr)
        if device:
            self.found[device.id] = device

    def error_received(self, exc: Exception) -> None:  # pragma: no cover
        log.debug("ssdp socket error", extra={"extra": {"error": str(exc)}})


async def ssdp_search(
    timeout_s: float = 3.0,
    interface_ip: str | None = None,
    *,
    repeat: int = MSEARCH_REPEAT,
    stagger_s: float = MSEARCH_STAGGER_S,
) -> list[DiscoveredDevice]:
    """Send each M-SEARCH ``repeat`` times, ``stagger_s`` apart, then collect until ``timeout_s``.

    UDP multicast drops packets; repeating the search is what every production SSDP client does.
    """
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _Collector, local_addr=(interface_ip or "0.0.0.0", 0), family=socket.AF_INET
    )
    try:
        for i in range(max(1, repeat)):
            for st in SEARCH_TARGETS:
                transport.sendto(build_msearch(st), SSDP_ADDR)
            if i < repeat - 1:
                await asyncio.sleep(stagger_s)
        await asyncio.sleep(timeout_s)
    finally:
        transport.close()
    return list(protocol.found.values())


def static_devices(entries: list[StaticDevice]) -> list[DiscoveredDevice]:
    return [
        DiscoveredDevice(
            id=e.id, vendor=e.vendor, ip=e.ip, name=e.name, model=e.model, source="static"
        )
        for e in entries
    ]


class Registry:
    def __init__(self) -> None:
        self._devices: dict[str, DiscoveredDevice] = {}

    def merge(self, devices: list[DiscoveredDevice]) -> list[str]:
        """Upsert devices; returns ids that are new to the registry."""
        new: list[str] = []
        for d in devices:
            if d.id not in self._devices:
                new.append(d.id)
            self._devices[d.id] = d
        return new

    def all(self) -> list[DiscoveredDevice]:
        return sorted(self._devices.values(), key=lambda d: (d.vendor, d.id))

    def by_vendor(self, vendor: Vendor) -> list[DiscoveredDevice]:
        return [d for d in self.all() if d.vendor == vendor]

    def get(self, device_id: str) -> DiscoveredDevice | None:
        return self._devices.get(device_id)

    def dump(self) -> list[dict[str, Any]]:
        return [d.model_dump(mode="json") for d in self.all()]


SearchFn = Callable[[], Any]


class DiscoveryService:
    """Seeds the registry from the static list, then runs SSDP periodically in the background."""

    def __init__(
        self,
        settings: Settings,
        registry: Registry | None = None,
        *,
        search: SearchFn | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry or Registry()
        self._search = search or (
            lambda: ssdp_search(settings.ssdp_timeout_s, settings.sonos_listener_host)
        )
        self._task: asyncio.Task[None] | None = None
        self.last_run: datetime | None = None
        self.last_error: str | None = None
        self._on_new: list[Callable[[list[DiscoveredDevice]], Awaitable[None] | None]] = []
        self.registry.merge(static_devices(settings.static_devices))

    def on_new_devices(
        self, callback: Callable[[list[DiscoveredDevice]], Awaitable[None] | None]
    ) -> None:
        """Called after each run with the devices that were not in the registry before."""
        self._on_new.append(callback)

    async def discover_once(self) -> list[str]:
        found = static_devices(self.settings.static_devices)
        try:
            found.extend(await self._search())
            self.last_error = None
        except Exception as exc:  # noqa: BLE001 - discovery must never crash the hub
            self.last_error = str(exc)
            log.warning("ssdp search failed", extra={"extra": {"error": str(exc)}})
        self.last_run = now()
        new = self.registry.merge(found)
        if new:
            log.info("discovered devices", extra={"extra": {"new": new}})
            devices = [d for d in (self.registry.get(i) for i in new) if d is not None]
            for cb in list(self._on_new):
                try:
                    result = cb(devices)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # noqa: BLE001 - a handoff failure must not stop discovery
                    log.exception("discovery callback failed")
        return new

    async def start(self) -> None:
        """Start the periodic loop without blocking the caller on the first SSDP window."""
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="discovery")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            self._task = None

    async def _loop(self) -> None:
        while True:
            await self.discover_once()
            await asyncio.sleep(self.settings.discovery_interval_s)
