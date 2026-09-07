from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from illyhub_hub.adapters.base import BaseAdapter
from illyhub_hub.api import (
    DENON_DISABLED_MSG,
    HEOS_DISABLED_MSG,
    HubRuntime,
    build_runtime,
    create_app,
    health_summary,
)
from illyhub_hub.config import Settings
from illyhub_hub.discovery import DiscoveryService
from illyhub_hub.state import ConnectionStatus, StateStore


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[tuple[httpx.AsyncClient, HubRuntime]]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://hub") as c:
            yield c, app.state.runtime


async def test_health_ok_with_fakes(client) -> None:
    c, rt = client
    r = await c.get("/api/health", headers={"x-correlation-id": "abc"})
    assert r.status_code == 200 and r.headers["x-correlation-id"] == "abc"
    body = r.json()
    assert body["status"] == "ok" and body["degraded"] is False and body["fake_devices"] is True
    assert set(body["connections"]) == {"heos", "sonos", "denon"}
    assert body["version"] and body["uptime_s"] >= 0 and body["state_version"] > 0
    await asyncio.sleep(0.01)  # discovery runs in the background, not during startup
    assert (await c.get("/api/health")).json()["discovery_last_run"] is not None


async def test_health_degrades_but_stays_200(client) -> None:
    c, rt = client
    rt.fakes.heos.drop("cable pulled")
    body = (await c.get("/api/health")).json()
    assert body["status"] == "degraded" and body["degraded"] is True
    assert body["last_error"] == "cable pulled"
    r = await c.get("/api/health")
    assert "x-correlation-id" in r.headers and len(r.headers["x-correlation-id"]) == 12


async def test_devices_lists_players_zones_sides(client) -> None:
    c, rt = client
    body = (await c.get("/api/devices")).json()
    assert [p["name"] for p in body["players"]] == ["Living Room Amp", "Kitchen", "Patio"]
    assert {z["key"] for z in body["zones"]} == {"main", "zone2"}
    assert all(z["id"].startswith("denon-fake:") for z in body["zones"])
    assert {s["id"] for s in body["sides"]} == {"heos:heos-1", rt.fakes.sonos.side_id()}
    assert body["sides"][0]["capabilities"]["supports_power"] is True
    assert body["discovered"] == []


async def test_openapi_has_tags(client) -> None:
    c, _rt = client
    spec = (await c.get("/openapi.json")).json()
    assert {t["name"] for t in spec["tags"]} == {"system", "devices"}
    assert "/api/health" in spec["paths"] and "/api/devices" in spec["paths"]


def test_health_summary_rules() -> None:
    conns = {
        "heos": ConnectionStatus(state="disabled", last_error=HEOS_DISABLED_MSG),
        "sonos": ConnectionStatus(state="connected"),
    }
    assert health_summary(conns, None) == (False, HEOS_DISABLED_MSG)  # disabled never degrades
    assert health_summary(conns, "ssdp blocked") == (
        False,
        "ssdp blocked",
    )  # discovery beats disabled
    conns["sonos"] = ConnectionStatus(state="reconnecting", last_error="lost")
    conns["denon"] = ConnectionStatus(state="reconnecting", last_error="denon down")
    assert health_summary(conns, "ssdp blocked") == (
        True,
        "denon down",
    )  # active adapters first, by name
    assert health_summary({}, None) == (False, None)


def test_build_runtime_real_adapters_without_hosts_marks_disabled() -> None:
    settings = Settings(_env_file=None, fake_devices=False)
    rt = build_runtime(settings)  # store mutated before any loop exists: must not raise
    assert [a.name for a in rt.adapters] == ["sonos"]
    assert rt.store.state.connections["heos"] == ConnectionStatus(
        state="disabled",
        last_error=HEOS_DISABLED_MSG,
        since=rt.store.state.connections["heos"].since,
    )
    assert rt.store.state.connections["denon"].last_error == DENON_DISABLED_MSG


def test_build_runtime_real_adapters_with_hosts() -> None:
    settings = Settings(
        _env_file=None, fake_devices=False, heos_host="10.0.0.5", denon_host="10.0.0.5"
    )
    rt = build_runtime(settings)
    assert sorted(a.name for a in rt.adapters) == ["denon", "heos", "sonos"]
    assert rt.fakes is None


class StubAdapter(BaseAdapter):
    name = "stub"

    def __init__(self, store: StateStore) -> None:
        super().__init__(store)
        self.calls: list[str] = []

    async def connect(self) -> None:
        self.calls.append("connect")
        self._set_status("connected")

    async def disconnect(self) -> None:
        self.calls.append("disconnect")


async def test_startup_does_not_block_on_discovery_window() -> None:
    settings = Settings(_env_file=None, discovery_interval_s=60)
    store = StateStore()
    stub = StubAdapter(store)
    gate = asyncio.Event()

    async def slow_search():
        await gate.wait()  # simulates the SSDP timeout window
        return []

    rt = HubRuntime(settings, store, [stub], DiscoveryService(settings, search=slow_search))
    app = create_app(settings, runtime_factory=lambda _s: rt)
    async with asyncio.timeout(1):
        async with app.router.lifespan_context(app):
            assert stub.calls == ["connect"]  # served before discovery finished
            assert rt.discovery.last_run is None
            gate.set()
            await asyncio.sleep(0.01)
            assert rt.discovery.last_run is not None
    assert stub.calls == ["connect", "disconnect"]
