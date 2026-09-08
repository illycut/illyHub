from __future__ import annotations

import asyncio

import httpx
import pytest

from illyhub_hub.adapters.denon import (
    DenonControlAdapter,
    decode_mv,
    encode_mv,
    level_to_step,
    parse_status_line,
    step_to_level,
    zone_id,
)
from illyhub_hub.config import Settings
from illyhub_hub.state import StateStore

HOST = "10.0.0.5"
MAIN = zone_id(HOST, "main")
ZONE2 = zone_id(HOST, "zone2")
MAIN_XML = "<root><Power><value>ON</value></Power></root>"
ZONE2_XML = "<root><Power><value>STANDBY</value></Power></root>"


class HttpRecorder:
    """The HTTP form API. ``forbidden`` reproduces current firmware, which 403s every path."""

    def __init__(
        self, forbidden: bool = False, fail_status: bool = False, zone2_missing: bool = False
    ) -> None:
        self.commands: list[bytes] = []
        self.forbidden = forbidden
        self.fail_status = fail_status
        self.zone2_missing = zone2_missing

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.forbidden:
            return httpx.Response(403, text="Error 403: Forbidden")
        if path == "/goform/formiPhoneAppDirect.xml":
            self.commands.append(request.url.query)
            return httpx.Response(200, text="")
        if self.fail_status:
            return httpx.Response(500)
        if "MainZone" in path:
            return httpx.Response(200, text=MAIN_XML)
        if "Zone2" in path:
            if self.zone2_missing:
                return httpx.Response(404)
            return httpx.Response(200, text=ZONE2_XML)
        return httpx.Response(404)


class FakeReceiver:
    """A telnet receiver that answers queries and volunteers status the way the real one does.

    Modelled on an AVR-X3400H: CR-terminated lines, a ``Z2OFF`` that drops the whole unit to
    standby, and silence for Zone 2 commands while Zone 2 is off.
    """

    def __init__(self, *, zone2_silent: bool = True) -> None:
        self.sent: list[str] = []
        self.zone2_silent = zone2_silent
        self.unit_power = "ON"
        self.main_power = "ON"
        self.main_mute = "OFF"
        self.main_volume = "36"
        self.zone2_power = "OFF"
        self.zone2_volume = "65"
        self.zone2_mute = "OFF"
        self._out: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = False

    # -- wire side ------------------------------------------------------------------

    def write(self, data: bytes) -> None:
        for command in data.decode().replace("\n", "").split("\r"):
            if command:
                self.sent.append(command)
                self._handle(command)

    def close(self) -> None:
        self.closed = True
        self._out.put_nowait(b"")  # unblock a pending readuntil so the reader can exit

    async def wait_closed(self) -> None:
        return None

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        data = await self._out.get()
        if data == b"":
            raise asyncio.IncompleteReadError(b"", None)
        return data

    async def read(self, n: int) -> bytes:  # pragma: no cover - only the overrun path uses this
        return b""

    def _emit(self, *lines: str) -> None:
        for line in lines:
            self._out.put_nowait(f"{line}\r".encode())

    # -- receiver behaviour ---------------------------------------------------------

    def _handle(self, command: str) -> None:
        if command == "PW?":
            self._emit(f"PW{'ON' if self.unit_power == 'ON' else 'STANDBY'}")
        elif command == "ZM?":
            self._emit(f"ZM{self.main_power}")
        elif command == "MV?":
            self._emit(f"MV{self.main_volume}", "MVMAX 98")
        elif command == "MU?":
            self._emit(f"MU{self.main_mute}")
        elif command == "Z2?":
            self._emit(f"Z2{self.zone2_power}", "Z2NET", f"Z2{self.zone2_volume}")
        elif command == "ZMON":
            self.unit_power, self.main_power = "ON", "ON"
            self._emit("PWON", "ZMON")
        elif command == "ZMOFF":
            self.main_power = "OFF"
            self._emit("ZMOFF")
        elif command == "Z2ON":
            self.unit_power, self.zone2_power = "ON", "ON"
            self._emit("PWON", "Z2ON")
        elif command == "Z2OFF":
            # The behaviour that forced read-back: turning Zone 2 off took the unit with it.
            self.zone2_power = "OFF"
            if self.main_power == "OFF":
                self.unit_power = "STANDBY"
                self._emit("PWSTANDBY", "Z2OFF")
            else:
                self._emit("Z2OFF")
        elif command in ("MUON", "MUOFF"):
            self.main_mute = command[2:]
            self._emit(command)
        elif command.startswith("MV") and command[2:].isdigit():
            self.main_volume = command[2:]
            self._emit(f"MV{self.main_volume}")
        elif command.startswith("Z2MU"):
            if not self.zone2_silent:
                self.zone2_mute = command[4:]
                self._emit(command)
        elif command.startswith("Z2") and command[2:].isdigit():
            self.zone2_volume = command[2:]
            self._emit(f"Z2{self.zone2_volume}")


def make(
    store: StateStore,
    rec: HttpRecorder,
    receiver: FakeReceiver | None = None,
    poll_s: float = 10.0,
    **settings_kw,
) -> DenonControlAdapter:
    settings = Settings(_env_file=None, denon_poll_s=poll_s, reconnect_max_s=0.01, **settings_kw)

    async def opener(host: str, port: int):
        if receiver is None:
            raise ConnectionRefusedError("busy")
        return receiver, receiver

    return DenonControlAdapter(
        store,
        settings,
        HOST,
        transport=httpx.MockTransport(rec.handler),
        telnet_opener=opener,
        player_ids=["heos-1"],
    )


async def settle(adapter: DenonControlAdapter, timeout: float = 1.0) -> None:
    """Wait for the telnet link to come up rather than sleeping a guessed interval."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if adapter._link is not None and adapter._link.connected:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("telnet link never came up")


# -- pure helpers ------------------------------------------------------------------------


def test_mv_encoding_matches_the_receiver_wire_format() -> None:
    # MV40 is 40; MV405 is the half step 40.5. Confirmed against the AVR-X3400H.
    assert decode_mv("40") == 40.0
    assert decode_mv("405") == 40.5
    assert decode_mv("00") == 0.0
    # The hub only ever sends whole steps, zero padded to two digits.
    assert encode_mv(40.0) == "40"
    assert encode_mv(40.4) == "40"
    assert encode_mv(7.0) == "07"
    # MV99 is rejected by the receiver, so encoding clamps at the 98 ceiling.
    assert encode_mv(120) == "98"
    assert encode_mv(-5) == "00"


def test_volume_scaling_caps_at_configured_max_not_plus_18db() -> None:
    # Hub 100 lands on the cap (60), never MV98, so a slider at full is not +18 dB.
    assert level_to_step(100, 60) == 60
    assert level_to_step(50, 60) == 30
    assert level_to_step(0, 60) == 0
    # Out-of-range levels clamp instead of overflowing the scale.
    assert level_to_step(150, 60) == 60
    assert level_to_step(-10, 60) == 0
    # A cap above the hardware ceiling still cannot exceed it.
    assert level_to_step(100, 200) == 98
    # Round trip.
    assert step_to_level(60, 60) == 100
    assert step_to_level(30, 60) == 50
    assert step_to_level(0, 60) == 0
    assert step_to_level(99, 0) == 0  # degenerate cap must not divide by zero


def test_parse_status_line_prefers_longer_patterns() -> None:
    # MVMAX must win over the MV-digits rule, and Z2MU over Z2 power.
    assert parse_status_line("MVMAX 98\r") == ("main_max", "98")
    assert parse_status_line("MV365\r") == ("main_volume", "365")
    assert parse_status_line("Z2MUON\r") == ("zone2_mute", "ON")
    assert parse_status_line("Z2ON\r") == ("zone2_power", "ON")
    assert parse_status_line("Z265\r") == ("zone2_volume", "65")
    # A fixed pre-out answers "--" rather than a level.
    assert parse_status_line("Z2--\r") == ("zone2_fixed", "--")
    assert parse_status_line("MV--\r") == ("main_fixed", "--")
    assert parse_status_line("PWSTANDBY\r") == ("unit_power", "STANDBY")
    assert parse_status_line("ZMOFF\r") == ("main_power", "OFF")
    assert parse_status_line("MUOFF\r") == ("main_mute", "OFF")
    # Everything else the receiver volunteers is ignored, not guessed at.
    assert parse_status_line("SSALSSET ON\r") is None
    assert parse_status_line("VSSCAUTO\r") is None
    assert parse_status_line("\r") is None


# -- adapter -----------------------------------------------------------------------------


async def test_connect_seeds_zones_reads_state_over_telnet_and_disconnects(
    store: StateStore,
) -> None:
    rec, receiver = HttpRecorder(forbidden=True), FakeReceiver()
    a = make(store, rec, receiver, poll_s=0.01)
    await a.connect()
    await a.connect()  # idempotent
    await settle(a)
    await a.refresh()
    main = store.state.zones[MAIN]
    assert main.power is True and main.key == "main" and main.host == HOST
    assert main.device_id == f"denon-{HOST}" and main.player_ids == ["heos-1"]
    # MVMAX from the receiver is ignored for scaling; the configured cap (60) is the scale.
    assert main.volume == step_to_level(36, 60) and main.muted is False
    assert a._max_step == 60 and a._reported_max == 98
    assert store.state.zones[ZONE2].power is False
    assert store.state.connections["denon"].state == "connected"
    # The 403 firmware must not be treated as a usable control path.
    assert a._http_ok is False and rec.commands == []
    await a.disconnect()
    assert a.status().state == "disconnected" and a._client is None


async def test_set_power_reads_back_so_a_zone2_side_effect_is_not_missed(
    store: StateStore,
) -> None:
    rec, receiver = HttpRecorder(forbidden=True), FakeReceiver()
    a = make(store, rec, receiver)
    events: list = []
    a.on_event(events.append)
    await a.connect()
    await settle(a)

    await a.set_power("main", False)  # bare key
    assert store.state.zones[MAIN].power is False
    # Zone 2 off with main already off standbys the unit; read-back sees both zones drop.
    await a.set_power(ZONE2, False)  # namespaced id
    assert receiver.sent[-4:-3] == ["Z2OFF"] or "Z2OFF" in receiver.sent
    assert store.state.zones[MAIN].power is False and store.state.zones[ZONE2].power is False

    await a.set_power("main", True)
    assert store.state.zones[MAIN].power is True
    power_events = [e for e in events if e.kind == "zone_power"]
    assert power_events[-1].payload == {"zone_id": MAIN, "power": True}

    with pytest.raises(ValueError):
        await a.set_power("zone9", True)
    with pytest.raises(ValueError):
        await a.set_power("denon-other:main", True)
    await a.disconnect()


async def test_set_volume_and_mute_over_telnet_with_readback(store: StateStore) -> None:
    rec, receiver = HttpRecorder(forbidden=True), FakeReceiver(zone2_silent=False)
    a = make(store, rec, receiver)
    events: list = []
    a.on_event(events.append)
    await a.connect()
    await settle(a)

    # 50 of 100 against the configured cap of 60 -> MV30, regardless of the MVMAX advertised.
    await a.set_volume("main", 50)
    assert "MV30" in receiver.sent
    assert store.state.zones[MAIN].volume == 50

    await a.set_mute("main", True)
    assert "MUON" in receiver.sent and store.state.zones[MAIN].muted is True
    await a.set_mute("main", False)
    assert store.state.zones[MAIN].muted is False

    await a.set_volume("zone2", 50)
    assert "Z230" in receiver.sent and store.state.zones[ZONE2].volume == 50
    await a.set_mute("zone2", True)
    assert "Z2MUON" in receiver.sent

    kinds = [e.kind for e in events]
    assert "zone_volume" in kinds and "zone_mute" in kinds
    await a.disconnect()


async def test_zone2_silence_while_off_is_not_an_error(store: StateStore) -> None:
    """The real receiver ignores Zone 2 commands while Zone 2 is off; that must not raise."""
    rec, receiver = HttpRecorder(forbidden=True), FakeReceiver(zone2_silent=True)
    a = make(store, rec, receiver)
    await a.connect()
    await settle(a)
    await a.set_mute("zone2", True)  # no reply from the receiver
    assert "Z2MUON" in receiver.sent
    await a.disconnect()


async def test_external_volume_change_reaches_hub_state(store: StateStore) -> None:
    """Somebody turning the knob on the receiver must show up in hub state."""
    rec, receiver = HttpRecorder(forbidden=True), FakeReceiver()
    a = make(store, rec, receiver)
    await a.connect()
    await settle(a)
    receiver._emit("MV20")
    expected = step_to_level(20, 60)
    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        if store.state.zones[MAIN].volume == expected:
            break
        await asyncio.sleep(0.005)
    assert store.state.zones[MAIN].volume == expected
    await a.disconnect()


async def test_http_fallback_used_when_the_receiver_still_answers_goform(
    store: StateStore,
) -> None:
    """Older firmware answers /goform/; telnet being unavailable must not disable control."""
    rec = HttpRecorder()  # not forbidden
    a = make(store, rec, receiver=None, poll_s=0.01)  # telnet opener refuses
    await a.connect()
    await asyncio.sleep(0.05)
    assert a._http_ok is True
    assert store.state.zones[MAIN].power is True
    assert store.state.zones[ZONE2].power is False
    await a.set_power("main", True)
    assert rec.commands[-1] == b"ZMON"  # exact query bytes, no trailing "="
    assert store.state.connections["denon"].state == "connected"
    await a.disconnect()


async def test_no_control_path_at_all_raises_rather_than_lying(store: StateStore) -> None:
    rec = HttpRecorder(forbidden=True)
    a = make(store, rec, receiver=None)  # 403 http and refused telnet
    await a.connect()
    await asyncio.sleep(0.05)
    with pytest.raises(RuntimeError, match="no control path"):
        await a.set_power("main", True)
    await a.disconnect()


async def test_all_zones_failing_marks_reconnecting_with_backoff(store: StateStore) -> None:
    rec = HttpRecorder(fail_status=True)
    a = make(store, rec, receiver=None, poll_s=0.01)
    await a.connect()
    await asyncio.sleep(0.05)
    assert store.state.connections["denon"].state == "reconnecting"
    assert "no zone answered" in (store.state.connections["denon"].last_error or "")
    await a.disconnect()


async def test_missing_zone2_goes_offline_after_threshold_without_failing_adapter(
    store: StateStore,
) -> None:
    rec = HttpRecorder(zone2_missing=True)
    a = make(store, rec, receiver=None, poll_s=10, denon_zone_fail_threshold=2)
    await a.connect()
    await asyncio.sleep(0.02)
    assert store.state.zones[ZONE2].online is True  # one failure is not enough
    await a.refresh()
    assert store.state.zones[ZONE2].online is False
    assert store.state.zones[MAIN].online is True and store.state.zones[MAIN].power is True
    assert store.state.connections["denon"].state == "connected"
    await a.disconnect()


async def test_telnet_disabled_falls_back_to_http_only(store: StateStore) -> None:
    rec, receiver = HttpRecorder(), FakeReceiver()
    a = make(store, rec, receiver, poll_s=0.01, denon_telnet=False)
    await a.connect()
    await asyncio.sleep(0.05)
    assert a._link is None and receiver.sent == []
    await a.set_power("main", True)
    assert rec.commands[-1] == b"ZMON"
    await a.disconnect()


async def test_link_recovers_when_the_receiver_drops_the_single_client(
    store: StateStore,
) -> None:
    """Port 23 allows one client, so a dropped link must be re-established, not abandoned."""
    receivers: list[FakeReceiver] = []

    async def opener(host: str, port: int):
        r = FakeReceiver()
        receivers.append(r)
        return r, r

    rec = HttpRecorder(forbidden=True)
    settings = Settings(_env_file=None, denon_poll_s=10, reconnect_max_s=0.01)
    a = DenonControlAdapter(
        store,
        settings,
        HOST,
        transport=httpx.MockTransport(rec.handler),
        telnet_opener=opener,
        player_ids=["heos-1"],
    )
    await a.connect()
    await settle(a)
    first = a._link
    receivers[0].close()  # receiver hangs up
    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        if a._link is not None and a._link is not first and a._link.connected:
            break
        await asyncio.sleep(0.005)
    assert a._link is not None and a._link is not first
    assert len(receivers) >= 2
    await a.disconnect()


async def test_player_ids_are_discovered_from_matching_heos_ip(store: StateStore) -> None:
    """Zones link to the HEOS player the receiver hosts without cross-adapter wiring."""
    from illyhub_hub.state import Player

    store.upsert_player(Player(id="heos-9", name="AVR", vendor="heos", ip=HOST))
    store.upsert_player(Player(id="heos-8", name="Elsewhere", vendor="heos", ip="10.0.0.9"))
    rec, receiver = HttpRecorder(forbidden=True), FakeReceiver()
    settings = Settings(_env_file=None, denon_poll_s=10, reconnect_max_s=0.01)

    async def opener(host: str, port: int):
        return receiver, receiver

    a = DenonControlAdapter(
        store,
        settings,
        HOST,
        transport=httpx.MockTransport(rec.handler),
        telnet_opener=opener,
    )
    await a.connect()
    await settle(a)
    assert store.state.zones[MAIN].player_ids == ["heos-9"]
    await a.disconnect()


async def test_fixed_zone_reports_no_volume_capability_and_refuses_commands(
    store: StateStore,
) -> None:
    """Zone 2 feeds an external amp on a fixed pre-out: the receiver cannot attenuate it.

    The UI hides the slider off these flags, and a command must fail loudly rather than be
    accepted and silently ignored by the hardware.
    """
    rec, receiver = HttpRecorder(forbidden=True), FakeReceiver()
    a = make(store, rec, receiver, denon_fixed_zones=["zone2"])
    await a.connect()
    await settle(a)
    assert store.state.zones[ZONE2].supports_volume is False
    assert store.state.zones[ZONE2].supports_mute is False
    # Main zone is unaffected.
    assert store.state.zones[MAIN].supports_volume is True
    with pytest.raises(ValueError, match="does not support volume"):
        await a.set_volume("zone2", 40)
    with pytest.raises(ValueError, match="does not support mute"):
        await a.set_mute("zone2", True)
    # Power still works: the zone plays, it just cannot be attenuated.
    await a.set_power("zone2", True)
    assert "Z2ON" in receiver.sent
    await a.disconnect()


async def test_receiver_reporting_fixed_output_clears_the_capability(store: StateStore) -> None:
    """Detection, not just config: a zone answering "--" self-corrects its flags."""
    rec, receiver = HttpRecorder(forbidden=True), FakeReceiver()
    a = make(store, rec, receiver)
    await a.connect()
    await settle(a)
    assert store.state.zones[ZONE2].supports_volume is True
    receiver._emit("Z2--")
    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        if store.state.zones[ZONE2].supports_volume is False:
            break
        await asyncio.sleep(0.005)
    assert store.state.zones[ZONE2].supports_volume is False
    await a.disconnect()
