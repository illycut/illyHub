from __future__ import annotations

import asyncio

import httpx
import pytest

from illyhub_hub.adapters.denon import HttpDenonAdapter, zone_id
from illyhub_hub.config import Settings
from illyhub_hub.state import StateStore

HOST = "10.0.0.5"
MAIN = zone_id(HOST, "main")
ZONE2 = zone_id(HOST, "zone2")
MAIN_XML = "<root><Power><value>ON</value></Power></root>"
ZONE2_XML = "<root><Power><value>STANDBY</value></Power></root>"


class Recorder:
    def __init__(self, fail_status: bool = False, zone2_missing: bool = False) -> None:
        self.commands: list[bytes] = []
        self.fail_status = fail_status
        self.zone2_missing = zone2_missing

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
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


def make(
    store: StateStore,
    rec: Recorder,
    poll_s: float = 0.01,
    telnet_opener=None,
    **settings_kw,
) -> HttpDenonAdapter:
    settings = Settings(_env_file=None, denon_poll_s=poll_s, reconnect_max_s=0.01, **settings_kw)
    return HttpDenonAdapter(
        store,
        settings,
        HOST,
        transport=httpx.MockTransport(rec.handler),
        telnet_opener=telnet_opener,
        player_ids=["heos-1"],
    )


async def test_connect_seeds_namespaced_zones_polls_status_and_disconnects(
    store: StateStore,
) -> None:
    rec = Recorder()
    a = make(store, rec)
    await a.connect()
    await a.connect()  # idempotent
    await asyncio.sleep(0.03)
    main = store.state.zones[MAIN]
    assert main.power is True and main.key == "main" and main.host == HOST
    assert main.device_id == f"denon-{HOST}" and main.player_ids == ["heos-1"]
    assert store.state.zones[ZONE2].power is False
    assert store.state.connections["denon"].state == "connected"
    await a.disconnect()
    assert a.status().state == "disconnected" and a._client is None


async def test_set_power_sends_bare_query_and_updates_zone(store: StateStore) -> None:
    rec = Recorder()
    a = make(store, rec)
    events = []
    a.on_event(events.append)
    await a.connect()
    await a.set_power("zone2", True)  # bare key
    await a.set_power(MAIN, False)  # namespaced id
    assert rec.commands == [b"Z2ON", b"ZMOFF"]  # exact query bytes, no trailing "="
    assert store.state.zones[ZONE2].power is True and store.state.zones[MAIN].power is False
    assert events[0].kind == "zone_power" and events[0].payload == {"zone_id": ZONE2, "power": True}
    with pytest.raises(ValueError):
        await a.set_power("zone9", True)
    with pytest.raises(ValueError):
        await a.set_power("denon-other:main", True)
    await a.disconnect()
    with pytest.raises(RuntimeError):
        await a.set_power("main", True)
    with pytest.raises(RuntimeError):
        await a.refresh()


async def test_all_zones_failing_marks_reconnecting_with_backoff(store: StateStore) -> None:
    rec = Recorder(fail_status=True)
    a = make(store, rec)
    await a.connect()
    await asyncio.sleep(0.03)
    assert store.state.connections["denon"].state == "reconnecting"
    assert "no zone answered" in (store.state.connections["denon"].last_error or "")
    await a.disconnect()


async def test_missing_zone2_goes_offline_after_threshold_without_failing_adapter(
    store: StateStore,
) -> None:
    rec = Recorder(zone2_missing=True)
    a = make(store, rec, poll_s=10, denon_zone_fail_threshold=2)
    await a.connect()
    await asyncio.sleep(0.01)
    assert store.state.zones[ZONE2].online is True  # one failure is not enough
    await a.refresh()
    assert store.state.zones[ZONE2].online is False
    assert store.state.zones[MAIN].online is True and store.state.zones[MAIN].power is True
    assert store.state.connections["denon"].state == "connected"
    await a.disconnect()


class CrReader:
    """Behaves like asyncio.StreamReader on a CR-only peer: readline() never returns."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.buf = b"".join(chunks)
        self.closed = False

    async def readline(self) -> bytes:  # pragma: no cover - proves readuntil is used
        await asyncio.sleep(3600)
        return b""

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        if len(self.buf) > 64:
            raise asyncio.LimitOverrunError("too long", 64)
        idx = self.buf.find(sep)
        if idx == -1:
            if self.closed:
                raise asyncio.IncompleteReadError(self.buf, None)
            await asyncio.sleep(0.01)
            self.closed = True
            raise asyncio.IncompleteReadError(self.buf, None)
        line, self.buf = self.buf[: idx + 1], self.buf[idx + 1 :]
        return line

    async def read(self, n: int) -> bytes:
        data, self.buf = self.buf[:n], self.buf[n:]
        return data


class FakeWriter:
    def __init__(self) -> None:
        self.closed = False
        self.waited = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


async def test_telnet_reads_cr_terminated_lines_tolerates_refusal_and_overrun(
    store: StateStore,
) -> None:
    attempts = {"n": 0}
    writers: list[FakeWriter] = []

    async def opener(host: str, port: int):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionRefusedError("busy")
        # Let the first HTTP status poll land first; otherwise its (truthful) STANDBY answer
        # races the telnet line and the assertion below would be order-dependent.
        while store.state.connections.get("denon", None) is None or (
            store.state.connections["denon"].state != "connected"
        ):
            await asyncio.sleep(0.001)
        if attempts["n"] == 2:
            reader = CrReader([b"Z2ON\r", b"garbage\r", b"ZMOFF\r"])
        elif attempts["n"] == 3:
            reader = CrReader([b"X" * 100 + b"\r"])  # oversized chunk → LimitOverrunError path
        else:
            reader = CrReader([])
        w = FakeWriter()
        writers.append(w)
        return reader, w

    rec = Recorder()
    a = make(store, rec, poll_s=10, telnet_opener=opener, denon_telnet=True)
    await a.connect()
    await asyncio.sleep(0.15)
    assert store.state.zones[ZONE2].power is True and store.state.zones[MAIN].power is False
    assert attempts["n"] >= 3 and all(w.closed and w.waited for w in writers[:1])
    await a.set_power("main", True)  # HTTP path unaffected by telnet state
    assert rec.commands[-1] == b"ZMON"
    await a.disconnect()
