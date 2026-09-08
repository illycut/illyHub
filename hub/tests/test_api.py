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
from illyhub_hub.commands import CommandRouter
from illyhub_hub.config import Settings
from illyhub_hub.discovery import DiscoveryService
from illyhub_hub.state import ConnectionStatus, StateStore


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[tuple[httpx.AsyncClient, HubRuntime]]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
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
    assert [p["name"] for p in body["players"]] == ["Den", "Living Room Amp", "Kitchen", "Patio"]
    assert {z["key"] for z in body["zones"]} == {"main", "zone2"}
    assert all(z["id"].startswith("denon-fake:") for z in body["zones"])
    assert {s["id"] for s in body["sides"]} == {
        "heos:heos-1",
        "heos:heos-2",
        rt.fakes.sonos.side_id(),
    }
    amp = next(s for s in body["sides"] if s["id"] == "heos:heos-1")
    assert amp["capabilities"]["supports_power"] is True
    assert body["discovered"] == []


async def test_openapi_has_tags(client) -> None:
    c, _rt = client
    spec = (await c.get("/openapi.json")).json()
    assert {t["name"] for t in spec["tags"]} >= {
        "system",
        "devices",
        "commands",
        "art",
        "dev",
        "auth",
        "browse",
        "play",
        "home",
        "settings",
    }
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

    rt = HubRuntime(
        settings,
        store,
        [stub],
        DiscoveryService(settings, search=slow_search),
        CommandRouter(store),
    )
    app = create_app(settings, runtime_factory=lambda _s: rt)
    async with asyncio.timeout(1):
        async with app.router.lifespan_context(app):
            assert stub.calls == ["connect"]  # served before discovery finished
            assert rt.discovery.last_run is None
            gate.set()
            await asyncio.sleep(0.01)
            assert rt.discovery.last_run is not None
    assert stub.calls == ["connect", "disconnect"]


# ---------------------------------------------------------------------------------------
# Phase 1: command endpoints, WebSocket, dev scenarios, discovery handoff
# ---------------------------------------------------------------------------------------

from illyhub_hub.adapters.fake import HEOS_PLAYER, KITCHEN, PATIO  # noqa: E402
from illyhub_hub.discovery import DiscoveredDevice  # noqa: E402


async def test_transport_volume_mute_endpoints_return_acks(client) -> None:
    c, rt = client
    r = await c.post(
        "/api/transport/play", json={"target": HEOS_PLAYER}, headers={"x-correlation-id": "c1"}
    )
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["correlation_id"] == "c1"
    assert body["action"] == "play" and body["state_version"] == rt.store.state.version
    assert rt.store.state.players[HEOS_PLAYER].play_state == "play"
    for action in ("pause", "toggle", "next", "prev", "stop"):
        assert (await c.post(f"/api/transport/{action}", json={"target": "all"})).status_code == 200
    assert (await c.post("/api/transport/dance", json={"target": "all"})).status_code == 422
    r = await c.post("/api/volume", json={"target": PATIO, "level": 42})
    assert r.status_code == 200 and rt.store.state.players[PATIO].volume == 42
    r = await c.post("/api/volume", json={"linked": True, "delta": 10})
    assert r.status_code == 200 and rt.store.state.players[PATIO].volume > 42
    assert (await c.post("/api/volume", json={"target": PATIO})).status_code == 422
    assert (await c.post("/api/volume", json={"linked": True})).status_code == 422
    r = await c.post("/api/mute", json={"target": KITCHEN, "muted": True})
    assert r.status_code == 200 and rt.store.state.players[KITCHEN].muted is True


async def test_error_envelope_status_codes(client) -> None:
    c, rt = client
    r = await c.post("/api/transport/play", json={"target": "ghost"})
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False and body["error"]["code"] == "unknown_target"
    assert body["error"]["correlation_id"] == r.headers["x-correlation-id"]
    r = await c.post("/api/seek", json={"target": HEOS_PLAYER, "position_ms": 1000})
    assert r.status_code == 409 and r.json()["error"]["code"] == "unsupported_action"
    r = await c.post("/api/seek", json={"target": KITCHEN, "position_ms": -5})
    assert r.status_code == 422  # pydantic ge=0 rejects it before the router does
    # Partial success: one side offline, the other applied -> 200 with a partial list.
    rt.store.mark_vendor_offline("heos")
    r = await c.post("/api/transport/play", json={"target": "all"})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert {p["target"] for p in body["partial"]} == {"heos:heos-1", "heos:heos-2"}
    assert body["partial"][0]["code"] == "device_offline"
    rt.fakes.sonos.drop()
    r = await c.post("/api/skip", json={"target": KITCHEN, "delta_ms": 15000})
    assert r.status_code == 503 and r.json()["error"]["code"] == "adapter_disconnected"


async def test_seek_skip_zone_power_group_endpoints(client) -> None:
    c, rt = client
    side = rt.fakes.sonos.side_id()
    await c.post("/api/transport/play", json={"target": KITCHEN})
    r = await c.post("/api/seek", json={"target": KITCHEN, "position_ms": 20000})
    assert r.status_code == 200 and rt.store.state.positions[side].position_ms == 20000
    r = await c.post("/api/skip", json={"target": KITCHEN})
    # the fake ticker runs at 50 Hz during the request, so allow one tick of drift
    assert r.status_code == 200 and 35000 <= rt.store.state.positions[side].position_ms < 35100
    r = await c.post("/api/zone/power", json={"zone_id": "denon-fake:zone2", "on": True})
    assert r.status_code == 200 and rt.store.state.zones["denon-fake:zone2"].power is True
    r = await c.delete(f"/api/group/{side}")
    assert r.status_code == 200 and rt.store.state.players[PATIO].group_id is None
    r = await c.post(
        "/api/group", json={"vendor": "sonos", "coordinator_id": PATIO, "member_ids": [KITCHEN]}
    )
    assert r.status_code == 200
    assert rt.store.state.players[KITCHEN].group_id == rt.fakes.sonos.group_id(PATIO)
    r = await c.post(
        "/api/zone/power", json={"zone_id": rt.fakes.sonos.side_id(PATIO), "on": False}
    )
    assert r.status_code == 200 and rt.store.state.players[KITCHEN].group_id is None


async def test_dev_scenarios_only_with_fakes(client) -> None:
    c, rt = client
    before = rt.store.state.now_playing["heos:heos-1"].title
    r = await c.post("/api/dev/fake/track_change")
    assert r.status_code == 200 and r.json()["scenario"] == "track_change"
    assert rt.store.state.now_playing["heos:heos-1"].title != before
    assert (await c.post("/api/dev/fake/volume_change")).json()["result"]["player"] == PATIO
    assert (await c.post("/api/dev/fake/disconnect_heos")).status_code == 200
    assert rt.store.state.connections["heos"].state == "reconnecting"
    assert (await c.post("/api/dev/fake/reconnect_heos")).status_code == 200
    assert rt.store.state.connections["heos"].state == "connected"
    assert (await c.post("/api/dev/fake/sonos_regroup")).json()["result"] == {"grouped": False}
    assert (await c.post("/api/dev/fake/sonos_regroup")).json()["result"] == {"grouped": True}
    r = await c.post("/api/dev/fake/nope")
    assert r.status_code == 404 and "track_change" in r.json()["scenarios"]
    # Not mounted for real adapters.
    real = create_app(
        Settings(_env_file=None),
        runtime_factory=lambda s: HubRuntime(
            s, StateStore(), [], DiscoveryService(s, search=_none), CommandRouter(StateStore())
        ),
    )
    assert not any(getattr(r, "path", "") == "/api/dev/fake/{scenario}" for r in real.routes)


async def _none():
    return []


async def test_command_error_handler_is_a_safety_net(client) -> None:
    """The router always acks, so a raw CommandError only escapes from a buggy route; the
    handler still turns it into an Ack envelope with the right status."""
    from illyhub_hub.commands import CommandError

    c, _rt = client
    app = c._transport.app  # type: ignore[attr-defined]

    async def broken() -> None:
        raise CommandError("device_offline", "Nope.", "x")

    app.add_api_route("/api/_broken", broken, methods=["GET"])
    # The static catch-all is registered last at app creation; a route added afterwards must be
    # moved ahead of it to be reachable, exactly as a real route would be.
    app.router.routes.insert(0, app.router.routes.pop())
    r = await c.get("/api/_broken")
    assert r.status_code == 409 and r.json()["error"]["code"] == "device_offline"
    assert r.json()["ok"] is False and r.json()["target"] == "x"


async def test_websocket_endpoint_streams_snapshot_delta_and_ack(client) -> None:
    """Drive the real /ws route at the ASGI level (starlette's TestClient is deprecated)."""
    import json

    c, rt = client
    app = c._transport.app  # type: ignore[attr-defined]
    inbox: asyncio.Queue[dict] = asyncio.Queue()
    outbox: asyncio.Queue[dict] = asyncio.Queue()

    async def receive() -> dict:
        return await inbox.get()

    async def send(message: dict) -> None:
        await outbox.put(message)

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "path": "/ws",
        "raw_path": b"/ws",
        "root_path": "",
        "scheme": "ws",
        "query_string": b"",
        "headers": [(b"host", b"localhost")],
        "client": ("test", 1),
        "server": ("localhost", 80),
        "subprotocols": [],
        "state": {},
    }
    inbox.put_nowait({"type": "websocket.connect"})
    task = asyncio.create_task(app(scope, receive, send))

    async def next_json() -> dict:
        msg = await asyncio.wait_for(outbox.get(), 1)
        assert msg["type"] == "websocket.send", msg
        return json.loads(msg["text"])

    assert (await asyncio.wait_for(outbox.get(), 1))["type"] == "websocket.accept"
    snap = await next_json()
    assert snap["type"] == "snapshot" and HEOS_PLAYER in snap["state"]["players"]
    r = await c.post("/api/volume", json={"target": KITCHEN, "level": 64})
    assert r.status_code == 200
    kinds = {}
    for _ in range(2):
        msg = await next_json()
        kinds[msg["type"]] = msg
    assert set(kinds) == {"delta", "ack"}
    assert kinds["delta"]["changed"][f"players.{KITCHEN}"]["volume"] == 64
    assert kinds["ack"]["correlation_id"] == r.headers["x-correlation-id"]
    inbox.put_nowait({"type": "websocket.receive", "text": json.dumps({"type": "ping"})})
    assert (await next_json())["type"] == "pong"
    inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task, 1)
    assert rt.store._listeners == [] or all(  # session unsubscribed on disconnect
        getattr(cb, "__self__", None) is None for cb in rt.store._listeners
    )


async def test_discovery_hands_first_heos_device_to_a_new_adapter() -> None:
    from test_heos import Generations

    gen = Generations()
    found = [DiscoveredDevice(id="heos-x", vendor="heos", ip="10.0.0.9", name="Amp")]
    settings = Settings(_env_file=None, discovery_interval_s=60, reconnect_max_s=0.05)

    async def search():
        return found

    async def no_sonos():
        from illyhub_hub.adapters.sonos import Topology

        return Topology()

    rt = build_runtime(
        settings,
        search=search,
        heos_factory=gen.factory,
        sonos_snapshot=no_sonos,
    )
    assert [a.name for a in rt.adapters] == ["sonos"]
    assert rt.store.state.connections["heos"].state == "disabled"
    await rt.discovery.discover_once()
    await asyncio.sleep(0.02)
    assert [a.name for a in rt.adapters] == ["sonos", "heos"]
    assert rt.adapters[1].host == "10.0.0.9"  # type: ignore[attr-defined]
    assert rt.store.state.connections["heos"].state == "connected"
    assert rt.router.playback["heos"] is rt.adapters[1]
    await rt.discovery.discover_once()  # a second run must not create a second socket
    assert len(rt.adapters) == 2
    for a in rt.adapters:
        await a.disconnect()
    await rt.router.aclose()
