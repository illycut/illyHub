"""Denon/Marantz zone control over the legacy telnet API, with the HTTP form API as a fallback.

Telnet (port 23) is the primary control and status path. The HTTP form endpoint
(``/goform/formiPhoneAppDirect.xml?ZMON``) is probed at connect and used only when the receiver
actually answers it, because it is gone on current firmware.

Verified against a Denon AVR-X3400H (firmware 3.139.173 / Aios 4.025) on the owner's LAN,
September 7 2026:

- The receiver serves nginx on 80/443 and returns 403 for **every** ``/goform/`` endpoint, so the
  HTTP control path does not work there at all. Telnet answers ``ZM?``/``PW?``/``MV?``/``MU?``
  and accepts commands.
- HEOS's own ``set_volume``/``get_volume`` are decoupled from the receiver's master volume on an
  AVR-hosted player: HEOS echoes ``success`` and applies nothing, and reports ``0`` no matter what
  ``MV`` really is. Volume and mute for that player therefore belong to this adapter, not to
  ``pyheos``.
- Zone power has cross-zone side effects: ``Z2OFF`` dropped the whole unit to ``PWSTANDBY`` while
  the main zone was the only other zone on. Every power command is read back rather than assumed.
- ``MV`` encodes half steps by appending a digit: ``MV40`` is 40, ``MV405`` is 40.5. ``MV98`` is
  the ceiling (``MV99`` is rejected) and spans -80 dB to +18 dB, hence ``denon_max_volume``.
- ``MVMAX`` is only trustworthy when the stream is read with CR framing; sleep-then-recv reads
  truncate mid-line and yield garbage. This adapter frames on CR and treats a missing ``MVMAX``
  as "use the configured cap".
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings
from ..state import StateStore, Zone
from ..tasks import stop_task
from .base import Backoff, DenonAdapter

TELNET_PORT = 23
COMMAND_PATH = "/goform/formiPhoneAppDirect.xml"
# Denon's absolute ceiling. MV99 is rejected by the receiver; MV98 is +18 dB.
MV_CEILING = 98
READ_LIMIT = 65536


@dataclass(frozen=True)
class ZoneSpec:
    """Per-zone command vocabulary.

    ``volume_prefix`` collides with ``power_prefix`` on Zone 2 (``Z2ON`` vs ``Z265``); digits
    versus letters disambiguate on parse.
    """

    name: str
    power_prefix: str  # ZM / Z2   -> ZMON, Z2OFF
    volume_prefix: str  # MV / Z2   -> MV35, Z265
    mute_prefix: str  # MU / Z2MU -> MUON, Z2MUOFF
    status_path: str  # HTTP fallback status document


ZONES: dict[str, ZoneSpec] = {
    "main": ZoneSpec("Main zone", "ZM", "MV", "MU", "/goform/formMainZone_MainZoneXml.xml"),
    "zone2": ZoneSpec("Zone 2", "Z2", "Z2", "Z2MU", "/goform/formZone2_Zone2Xml.xml"),
}

_POWER_RE = re.compile(r"<Power>\s*<value>(ON|OFF|STANDBY)</value>", re.IGNORECASE)

TelnetOpener = Callable[[str, int], Any]


def zone_id(host: str, key: str) -> str:
    return f"denon-{host}:{key}"


# -- volume encoding ---------------------------------------------------------------------


def decode_mv(raw: str) -> float:
    """``"40"`` -> 40.0, ``"405"`` -> 40.5. Denon appends a digit for the half step."""
    return int(raw) / 10 if len(raw) == 3 else float(int(raw))


def encode_mv(step: float) -> str:
    """Whole steps only, zero padded. Half steps are readable but the hub never sends them."""
    return f"{int(round(min(max(step, 0), MV_CEILING))):02d}"


def level_to_step(level: int, max_step: int) -> float:
    """Hub 0-100 onto 0-``max_step``, so slider 100 is the configured cap, not +18 dB."""
    return min(max(level, 0), 100) / 100 * min(max_step, MV_CEILING)


def step_to_level(step: float, max_step: int) -> int:
    ceiling = min(max_step, MV_CEILING)
    if ceiling <= 0:
        return 0
    return min(max(round(step / ceiling * 100), 0), 100)


# -- status line parsing -----------------------------------------------------------------

_LINE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^PW(ON|STANDBY)$"), "unit_power"),
    (re.compile(r"^ZM(ON|OFF)$"), "main_power"),
    (re.compile(r"^MU(ON|OFF)$"), "main_mute"),
    (re.compile(r"^MVMAX (\d+)$"), "main_max"),
    (re.compile(r"^MV(\d+)$"), "main_volume"),
    (re.compile(r"^Z2MU(ON|OFF)$"), "zone2_mute"),
    (re.compile(r"^Z2(ON|OFF)$"), "zone2_power"),
    (re.compile(r"^Z2(\d+)$"), "zone2_volume"),
    # A zone on a fixed pre-out answers "--" instead of a level; that zone is not attenuable.
    (re.compile(r"^MV(--)$"), "main_fixed"),
    (re.compile(r"^Z2(--)$"), "zone2_fixed"),
)


def parse_status_line(line: str) -> tuple[str, str] | None:
    """One CR-terminated receiver line to ``(key, raw_value)``. Unknown chatter returns None.

    Rule order matters: ``MVMAX 78`` must be tried before the ``MV`` digits rule, and
    ``Z2MUON`` before ``Z2(ON|OFF)``, or the shorter pattern swallows the longer one.
    """
    text = line.strip("\r\n ")
    for pattern, key in _LINE_RULES:
        m = pattern.match(text)
        if m:
            return key, m.group(1)
    return None


# -- telnet link -------------------------------------------------------------------------


class TelnetLink:
    """The single telnet conversation with the receiver.

    Port 23 accepts one client at a time, so this owns both commands and the status stream: a
    lone reader task frames on CR and fans every line out to the state callback and to whatever
    :meth:`request` calls are waiting. Commands are serialised behind a lock so two callers
    cannot interleave on the wire.
    """

    def __init__(
        self,
        host: str,
        opener: TelnetOpener,
        on_line: Callable[[str, str], None],
        log: Any,
        *,
        port: int = TELNET_PORT,
    ) -> None:
        self.host, self.port = host, port
        self._opener = opener
        self._on_line = on_line
        self.log = log
        self._reader: Any = None
        self._writer: Any = None
        self._read_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._waiters: list[tuple[set[str], dict[str, str], asyncio.Event]] = []
        self.connected = False
        # Set when the reader stops for any reason, so the link loop can wait instead of poll.
        self.closed = asyncio.Event()

    async def open(self) -> None:
        self._reader, self._writer = await self._opener(self.host, self.port)
        self.connected = True
        self._read_task = asyncio.create_task(self._read_loop(), name="denon-telnet-read")

    async def close(self) -> None:
        self.connected = False
        self.closed.set()
        await stop_task(self._read_task)
        self._read_task = None
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                self.log.debug("telnet close raised", exc_info=True)
        for _keys, _got, event in list(self._waiters):
            event.set()  # release anyone mid-request instead of making them wait out the timeout
        self._waiters.clear()

    async def send(self, command: str) -> None:
        if not self.connected or self._writer is None:
            raise ConnectionError("telnet link is not open")
        async with self._send_lock:
            # The receiver terminates on CR only; LF is not accepted.
            self._writer.write((command + "\r").encode())
            drain = getattr(self._writer, "drain", None)
            if drain is not None:
                await drain()

    async def request(
        self,
        command: str,
        keys: set[str],
        # The timeout is this API's contract: a silent receiver is a normal answer, not an error.
        timeout: float = 3.0,  # noqa: ASYNC109
    ) -> dict[str, str]:
        """Send ``command`` and collect the lines whose parsed keys are in ``keys``.

        Returns whatever arrived before ``timeout``. A receiver that stays silent -- Zone 2
        commands while Zone 2 is off, for instance -- yields an empty dict rather than an error.
        """
        got: dict[str, str] = {}
        event = asyncio.Event()
        waiter = (keys, got, event)
        self._waiters.append(waiter)
        try:
            await self.send(command)
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except TimeoutError:
                pass
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
        return dict(got)

    def _dispatch(self, key: str, value: str) -> None:
        for keys, got, event in list(self._waiters):
            if key in keys:
                got[key] = value
                if keys <= got.keys():
                    event.set()
        try:
            self._on_line(key, value)
        except Exception:  # noqa: BLE001 - a bad state update must not kill the reader
            self.log.exception("denon state update failed", extra={"extra": {"key": key}})

    async def _read_loop(self) -> None:
        reader = self._reader
        while reader is not None:
            try:
                raw = await reader.readuntil(b"\r")
            except asyncio.IncompleteReadError:
                break  # peer closed
            except asyncio.LimitOverrunError:
                await reader.read(READ_LIMIT)  # discard the oversized chunk and keep going
                continue
            parsed = parse_status_line(raw.decode(errors="ignore"))
            if parsed is not None:
                self._dispatch(*parsed)
        self.connected = False
        self.closed.set()


class DenonControlAdapter(DenonAdapter):
    """Zone power, volume and mute for one receiver, plus the HEOS player it hosts."""

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
        self._explicit_player_ids = player_ids
        self._link: TelnetLink | None = None
        self._link_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._backoff = Backoff(cap=settings.reconnect_max_s)
        self._zone_failures: dict[str, int] = dict.fromkeys(ZONES, 0)
        self._http_ok = False
        # Hard cap from config; the receiver cannot raise it (see _apply/main_max).
        self._max_step = min(settings.denon_max_volume, MV_CEILING)
        self._reported_max: float | None = None
        self._closing = False

    # -- lifecycle ------------------------------------------------------------------

    async def connect(self) -> None:
        if self._poll_task is not None:
            self.log.debug("connect() ignored; already connected")
            return
        self._closing = False
        self._client = httpx.AsyncClient(
            base_url=f"http://{self.host}", timeout=5.0, transport=self._transport
        )
        for key in ZONES:
            if self.zone_id(key) not in self.store.state.zones:
                self.store.set_zone(self._zone(key))
        self._http_ok = await self._probe_http()
        if self.settings.denon_telnet:
            self._link_task = asyncio.create_task(self._link_loop(), name="denon-telnet")
        self._poll_task = asyncio.create_task(self._poll_loop(), name="denon-poll")

    async def disconnect(self) -> None:
        self._closing = True
        await stop_task(self._poll_task)
        await stop_task(self._link_task)
        self._poll_task = self._link_task = None
        link, self._link = self._link, None
        if link is not None:
            await link.close()
        await self._cancel_tasks()
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
        self._set_status("disconnected")

    async def _probe_http(self) -> bool:
        """Current firmware answers 403 for every ``/goform/`` path; older models answer XML."""
        if self._client is None:
            return False
        try:
            resp = await self._client.get(ZONES["main"].status_path)
            ok = resp.status_code == 200 and _POWER_RE.search(resp.text) is not None
        except Exception as exc:  # noqa: BLE001
            self.log.info("denon http probe failed", extra={"extra": {"error": str(exc)}})
            return False
        if not ok:
            self.log.info(
                "denon http control unavailable; using telnet only",
                extra={"extra": {"status": resp.status_code}},
            )
        return ok

    # -- identity helpers -----------------------------------------------------------

    def zone_id(self, key: str) -> str:
        return zone_id(self.host, key)

    def _player_ids(self) -> list[str]:
        """The HEOS player this receiver hosts, matched on IP so no cross-adapter wiring is
        needed. An explicit list (tests, config) wins."""
        if self._explicit_player_ids is not None:
            return list(self._explicit_player_ids)
        return sorted(
            pid
            for pid, p in self.store.state.players.items()
            if p.vendor == "heos" and p.ip == self.host
        )

    def _zone(self, key: str, **fields: Any) -> Zone:
        existing = self.store.state.zones.get(self.zone_id(key))
        attenuable = key not in self.settings.denon_fixed_zones
        base = existing or Zone(
            id=self.zone_id(key),
            key=key,
            name=ZONES[key].name,
            host=self.host,
            device_id=f"denon-{self.host}",
            supports_volume=attenuable,
            supports_mute=attenuable,
        )
        players = self._player_ids()
        if players and base.player_ids != players:
            base = base.model_copy(update={"player_ids": players})
        return base.model_copy(update=fields) if fields else base

    def _key_for(self, zone_ref: str) -> str:
        """Accept either a bare key (``main``) or a namespaced id (``denon-host:main``)."""
        key = zone_ref.rsplit(":", 1)[-1]
        if key not in ZONES or (":" in zone_ref and zone_ref != self.zone_id(key)):
            raise ValueError(f"unknown zone {zone_ref!r}")
        return key

    # -- commands -------------------------------------------------------------------

    async def set_power(self, zone_ref: str, on: bool) -> None:
        """Power one zone, then read the receiver back.

        Read-back is not defensive padding: ``Z2OFF`` was observed dropping the whole unit to
        standby, so an optimistic write would have been wrong for the *other* zone.
        """
        key = self._key_for(zone_ref)
        await self._command(f"{ZONES[key].power_prefix}{'ON' if on else 'OFF'}")
        await self._read_back_power()
        zone = self.store.state.zones.get(self.zone_id(key))
        self._emit("zone_power", zone_id=self.zone_id(key), power=bool(zone and zone.power))

    async def set_volume(self, zone_ref: str, level: int) -> None:
        """Set zone volume from a hub 0-100 level, capped by ``denon_max_volume``."""
        key = self._key_for(zone_ref)
        self._require_capability(key, "supports_volume", "volume")
        step = level_to_step(level, self._max_step)
        await self._command(f"{ZONES[key].volume_prefix}{encode_mv(step)}")
        await self._read_back_volume(key)
        self._emit("zone_volume", zone_id=self.zone_id(key), level=level)

    async def set_mute(self, zone_ref: str, on: bool) -> None:
        key = self._key_for(zone_ref)
        self._require_capability(key, "supports_mute", "mute")
        await self._command(f"{ZONES[key].mute_prefix}{'ON' if on else 'OFF'}")
        await self._read_back_volume(key)
        self._emit("zone_mute", zone_id=self.zone_id(key), muted=on)

    def _require_capability(self, key: str, flag: str, what: str) -> None:
        """A fixed pre-out zone must fail loudly, not accept a command the hardware ignores."""
        zone = self.store.state.zones.get(self.zone_id(key))
        if zone is not None and not getattr(zone, flag):
            raise ValueError(f"zone {key!r} does not support {what} (fixed output)")

    async def _command(self, command: str) -> None:
        """Telnet first. HTTP is only attempted when the connect-time probe said it works."""
        link = self._link
        if link is not None and link.connected:
            await link.send(command)
            return
        if self._http_ok and self._client is not None:
            # The receiver expects the bare command as the query string: ``?ZMON``, not ``?ZMON=``.
            resp = await self._client.get(f"{COMMAND_PATH}?{command}")
            resp.raise_for_status()
            return
        raise RuntimeError("denon adapter has no control path (telnet down, http unavailable)")

    # -- status ---------------------------------------------------------------------

    def _apply(self, key: str, value: str) -> None:
        """Fold one parsed status line into the store.

        This is also the path by which somebody turning the knob on the receiver itself reaches
        hub state, so it must handle every line the receiver volunteers, not just replies.
        """
        if key == "main_max":
            # Recorded, never applied. This receiver reports MVMAX inconsistently (75, 82, 79,
            # 665 seen across successive reads), and a cap the hardware can raise is not a
            # safety cap: denon_max_volume stays authoritative so levels stay reproducible.
            self._reported_max = decode_mv(value)
            return
        if key.endswith("_fixed"):
            zone_key = "zone2" if key.startswith("zone2") else "main"
            self.store.set_zone(
                self._zone(zone_key, supports_volume=False, supports_mute=False, online=True)
            )
            return
        if key == "unit_power":
            if value == "STANDBY":  # standby takes every zone with it
                for k in ZONES:
                    self.store.set_zone(self._zone(k, power=False, online=True))
            return
        zone_key = "zone2" if key.startswith("zone2") else "main"
        if key.endswith("_power"):
            self.store.set_zone(self._zone(zone_key, power=value == "ON", online=True))
        elif key.endswith("_mute"):
            self.store.set_zone(self._zone(zone_key, muted=value == "ON", online=True))
        elif key.endswith("_volume"):
            level = step_to_level(decode_mv(value), self._max_step)
            self.store.set_zone(self._zone(zone_key, volume=level, online=True))

    async def _read_back_power(self) -> None:
        link = self._link
        if link is None or not link.connected:
            return
        await link.request("PW?", {"unit_power"})
        await link.request("ZM?", {"main_power"})
        await link.request("Z2?", {"zone2_power"})

    async def _read_back_volume(self, key: str) -> None:
        link = self._link
        if link is None or not link.connected:
            return
        if key == "main":
            await link.request("MV?", {"main_volume"})
            await link.request("MU?", {"main_mute"})
        else:
            await link.request("Z2?", {"zone2_volume"})

    async def refresh(self) -> bool:
        """Poll every zone. Returns True when at least one zone answered."""
        link = self._link
        if link is not None and link.connected:
            answered = await link.request("PW?", {"unit_power"})
            for query, keys in (
                ("ZM?", {"main_power"}),
                ("MV?", {"main_volume"}),
                ("MU?", {"main_mute"}),
                ("Z2?", {"zone2_power"}),
            ):
                answered |= await link.request(query, keys)
            if answered:
                for key in ZONES:
                    self._zone_failures[key] = 0
                return True
        if self._http_ok:
            return await self._refresh_http()
        return False

    async def _refresh_http(self) -> bool:
        if self._client is None:
            raise RuntimeError("denon adapter is not connected")
        any_ok = False
        for key, spec in ZONES.items():
            try:
                resp = await self._client.get(spec.status_path)
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
                self._note_zone_failure(key, exc)
        return any_ok

    def _note_zone_failure(self, key: str, exc: Exception) -> None:
        self._zone_failures[key] += 1
        if self._zone_failures[key] == self.settings.denon_zone_fail_threshold:
            self.log.warning("zone unavailable", extra={"extra": {"zone": key, "error": str(exc)}})
            self.store.set_zone(self._zone(key, online=False))

    # -- loops ----------------------------------------------------------------------

    async def _link_loop(self) -> None:
        """Keep the single telnet conversation alive.

        Port 23 allows one client, so a busy receiver -- the HEOS app, a Home Assistant, a stale
        session -- surfaces here as a refused connection and is retried with backoff rather than
        failing the adapter.
        """
        backoff = Backoff(cap=self.settings.reconnect_max_s)
        while not self._closing:
            link = TelnetLink(self.host, self._telnet_opener, self._apply, self.log)
            try:
                await link.open()
                self._link = link
                backoff.reset()
                await link.request("MV?", {"main_volume", "main_max"})
                self._emit("telnet_up", host=self.host)
                await link.closed.wait()
            except asyncio.CancelledError:
                await link.close()
                raise
            except Exception as exc:  # noqa: BLE001 - telnet may simply be busy
                self.log.info("telnet unavailable", extra={"extra": {"error": str(exc)}})
            finally:
                if self._link is link:
                    self._link = None
                await link.close()
            if self._closing:
                return
            await asyncio.sleep(backoff.next())

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
