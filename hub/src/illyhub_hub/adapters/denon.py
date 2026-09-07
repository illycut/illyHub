"""Denon/Marantz zone power over the legacy control API.

Commands go over HTTP (``/goform/formiPhoneAppDirect.xml?ZMON``), which is connectionless and
sidesteps the receiver's single-client telnet limit. Status is polled per zone from the zone
XML endpoints; a zone that keeps failing (e.g. a receiver without Zone 2) is marked offline
rather than failing the adapter. An optional telnet reader (port 23) streams CR-terminated
status lines; if it cannot connect (busy/refused) the HTTP path keeps working.

Verified only against ``httpx.MockTransport`` and a fake stream reader; no receiver exercised.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

import httpx

from ..config import Settings
from ..state import StateStore, Zone
from ..tasks import stop_task
from .base import Backoff, DenonAdapter

ZONES: dict[str, tuple[str, str, str]] = {
    # zone key: (display name, command prefix, status XML path)
    "main": ("Main zone", "ZM", "/goform/formMainZone_MainZoneXml.xml"),
    "zone2": ("Zone 2", "Z2", "/goform/formZone2_Zone2Xml.xml"),
}
COMMAND_PATH = "/goform/formiPhoneAppDirect.xml"
_POWER_RE = re.compile(r"<Power>\s*<value>(ON|OFF|STANDBY)</value>", re.IGNORECASE)
_TELNET_RE = re.compile(r"^(ZM|Z2)(ON|OFF)$")

TelnetOpener = Callable[[str, int], Any]
ClientFactory = Callable[[str], httpx.AsyncClient]


def zone_id(host: str, key: str) -> str:
    return f"denon-{host}:{key}"


class HttpDenonAdapter(DenonAdapter):
    def __init__(
        self,
        store: StateStore,
        settings: Settings,
        host: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        telnet_opener: TelnetOpener | None = None,
        player_ids: list[str] | None = None,
    ) -> None:
        super().__init__(store)
        self.settings = settings
        self.host = host
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._telnet_opener = telnet_opener or asyncio.open_connection
        self._player_ids = player_ids or []
        self._poll_task: asyncio.Task[None] | None = None
        self._telnet_task: asyncio.Task[None] | None = None
        self._backoff = Backoff(cap=settings.reconnect_max_s)
        self._zone_failures: dict[str, int] = dict.fromkeys(ZONES, 0)

    # -- lifecycle ------------------------------------------------------------------

    async def connect(self) -> None:
        if self._poll_task is not None:
            return
        self._client = httpx.AsyncClient(
            base_url=f"http://{self.host}", timeout=5.0, transport=self._transport
        )
        for key in ZONES:
            if self.zone_id(key) not in self.store.state.zones:
                self.store.set_zone(self._zone(key))
        self._poll_task = asyncio.create_task(self._poll_loop(), name="denon-poll")
        if self.settings.denon_telnet:
            self._telnet_task = asyncio.create_task(self._telnet_loop(), name="denon-telnet")

    async def disconnect(self) -> None:
        await stop_task(self._poll_task)
        await stop_task(self._telnet_task)
        self._poll_task = self._telnet_task = None
        await self._cancel_tasks()
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
        self._set_status("disconnected")

    def zone_id(self, key: str) -> str:
        return zone_id(self.host, key)

    def _zone(self, key: str, **fields: Any) -> Zone:
        existing = self.store.state.zones.get(self.zone_id(key))
        base = existing or Zone(
            id=self.zone_id(key),
            key=key,
            name=ZONES[key][0],
            host=self.host,
            device_id=f"denon-{self.host}",
            player_ids=list(self._player_ids),
        )
        return base.model_copy(update=fields) if fields else base

    def _key_for(self, zone_ref: str) -> str:
        """Accept either a bare key (``main``) or a namespaced id (``denon-host:main``)."""
        key = zone_ref.rsplit(":", 1)[-1]
        if key not in ZONES or (":" in zone_ref and zone_ref != self.zone_id(key)):
            raise ValueError(f"unknown zone {zone_ref!r}")
        return key

    # -- commands -------------------------------------------------------------------

    async def set_power(self, zone_ref: str, on: bool) -> None:
        key = self._key_for(zone_ref)
        await self._send(f"{ZONES[key][1]}{'ON' if on else 'OFF'}")
        self.store.set_zone(self._zone(key, power=on))
        self._emit("zone_power", zone_id=self.zone_id(key), power=on)

    async def _send(self, command: str) -> None:
        if self._client is None:
            raise RuntimeError("denon adapter is not connected")
        # The receiver expects the bare command as the query string: ``?ZMON``, not ``?ZMON=``.
        resp = await self._client.get(f"{COMMAND_PATH}?{command}")
        resp.raise_for_status()

    # -- status ---------------------------------------------------------------------

    async def refresh(self) -> bool:
        """Poll every zone. Returns True when at least one zone answered."""
        if self._client is None:
            raise RuntimeError("denon adapter is not connected")
        any_ok = False
        for key, (_, _, path) in ZONES.items():
            try:
                resp = await self._client.get(path)
                resp.raise_for_status()
                match = _POWER_RE.search(resp.text)
                if match is None:
                    raise ValueError("no <Power> element in zone status")
                self._zone_failures[key] = 0
                self.store.set_zone(
                    self._zone(key, power=match.group(1).upper() == "ON", online=True)
                )
                any_ok = True
            except Exception as exc:  # noqa: BLE001 - one zone must not fail the adapter
                self._zone_failures[key] += 1
                if self._zone_failures[key] == self.settings.denon_zone_fail_threshold:
                    self.log.warning(
                        "zone unavailable", extra={"extra": {"zone": key, "error": str(exc)}}
                    )
                    self.store.set_zone(self._zone(key, online=False))
        return any_ok

    async def _poll_loop(self) -> None:
        while True:
            try:
                if await self.refresh():
                    self._backoff.reset()
                    self._set_status("connected")
                    await asyncio.sleep(self.settings.denon_poll_s)
                else:
                    self._set_status("reconnecting", "no zone answered status poll")
                    await asyncio.sleep(self._backoff.next())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._set_status("reconnecting", str(exc))
                await asyncio.sleep(self._backoff.next())

    async def _telnet_loop(self) -> None:
        backoff = Backoff(cap=self.settings.reconnect_max_s)
        while True:
            try:
                reader, writer = await self._telnet_opener(self.host, 23)
                backoff.reset()
                try:
                    await self._read_telnet(reader)
                finally:
                    writer.close()
                    await writer.wait_closed()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - telnet is best-effort
                self.log.info("telnet unavailable", extra={"extra": {"error": str(exc)}})
            await asyncio.sleep(backoff.next())

    async def _read_telnet(self, reader: Any) -> None:
        """Denon terminates each status line with CR only, so ``readline`` would block."""
        while True:
            try:
                raw = await reader.readuntil(b"\r")
            except asyncio.IncompleteReadError:
                return  # peer closed
            except asyncio.LimitOverrunError:
                await reader.read(65536)  # discard the oversized chunk and keep going
                continue
            self._apply_telnet_line(raw.decode(errors="ignore").strip("\r\n"))

    def _apply_telnet_line(self, line: str) -> None:
        match = _TELNET_RE.match(line)
        if not match:
            return
        key = "main" if match.group(1) == "ZM" else "zone2"
        on = match.group(2) == "ON"
        self.store.set_zone(self._zone(key, power=on, online=True))
        self._emit("zone_power", zone_id=self.zone_id(key), power=on)
