from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from illyhub_hub.adapters.fake import HEOS_PLAYER, KITCHEN, PATIO, FakeBundle
from illyhub_hub.commands import CommandRouter
from illyhub_hub.state import StateStore
from illyhub_hub.ws import ClientSession, delta_message, lookup


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False
        self.got = asyncio.Event()

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)
        self.got.set()

    async def receive_text(self) -> str:
        item = await self.inbox.get()
        return item if isinstance(item, str) else json.dumps(item)

    async def close(self, code: int = 1000) -> None:
        self.closed = True

    async def wait_for(self, n: int) -> None:
        for _ in range(200):
            if len(self.sent) >= n:
                return
            await asyncio.sleep(0.001)
        raise AssertionError(f"only {len(self.sent)} messages: {[m['type'] for m in self.sent]}")


def test_lookup_and_delta_shape() -> None:
    dump = {"version": 3, "players": {"a": {"volume": 1}}, "sync": {"status": "idle"}}
    assert lookup(dump, "players.a") == {"volume": 1}
    assert lookup(dump, "players.gone") is None
    assert lookup(dump, "sync") == {"status": "idle"}
    msg = delta_message(2, dump, ["players.a", "players.gone", "sync"])
    assert msg["from_version"] == 2 and msg["to_version"] == 3
    assert msg["changed"] == {
        "players.a": {"volume": 1},
        "players.gone": None,
        "sync": {"status": "idle"},
    }


@pytest.fixture
async def rig(store: StateStore):
    b = FakeBundle(store, tick_interval_s=10)
    await b.start()
    router = CommandRouter(store, playback={"heos": b.heos, "sonos": b.sonos})
    yield store, b, router
    await router.aclose()
    await b.stop()


async def test_snapshot_then_ordered_deltas_then_ack(rig) -> None:
    store, b, router = rig
    sock = FakeSocket()
    session = ClientSession(sock, store, ping_interval_s=60)
    task = asyncio.create_task(session.run(router))
    await sock.wait_for(1)
    snap = sock.sent[0]
    assert snap["type"] == "snapshot" and snap["version"] == store.state.version
    assert snap["state"]["players"][HEOS_PLAYER]["name"] == "Living Room Amp"
    v0 = snap["version"]
    b.sonos.sim_volume(KITCHEN, 77)  # coordinator: the side's derived volume changes too
    b.heos.sim_play_state(HEOS_PLAYER, "play")
    await sock.wait_for(3)
    d1, d2 = sock.sent[1], sock.sent[2]
    assert d1["type"] == "delta" and d1["from_version"] == v0 and d1["to_version"] == v0 + 1
    assert d1["changed"][f"players.{KITCHEN}"]["volume"] == 77
    assert set(d1["changed"]) == {f"players.{KITCHEN}", f"sides.{b.sonos.side_id()}"}
    assert d1["changed"][f"sides.{b.sonos.side_id()}"]["volume"] == 48  # member average
    assert d2["from_version"] == v0 + 1 and d2["to_version"] == v0 + 2
    ack = await router.transport(HEOS_PLAYER, "pause")
    await sock.wait_for(5)
    kinds = [m["type"] for m in sock.sent]
    assert "ack" in kinds
    ack_msg = next(m for m in sock.sent if m["type"] == "ack")
    assert ack_msg["ok"] is True and ack_msg["correlation_id"] == ack.correlation_id
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert sock.closed


async def test_client_ping_pong_resync_and_bad_message(rig) -> None:
    store, _b, router = rig
    sock = FakeSocket()
    session = ClientSession(sock, store, ping_interval_s=60)
    task = asyncio.create_task(session.run(router))
    await sock.wait_for(1)
    sock.inbox.put_nowait({"type": "ping"})
    sock.inbox.put_nowait({"type": "resync"})
    sock.inbox.put_nowait({"type": "dance"})
    sock.inbox.put_nowait('"garbage"')  # valid JSON, not an object
    sock.inbox.put_nowait("{not json at all")  # malformed bytes: connection must survive
    sock.inbox.put_nowait({"type": "ping"})
    await sock.wait_for(7)
    kinds = [m["type"] for m in sock.sent[1:]]
    assert kinds == ["pong", "snapshot", "error", "error", "error", "pong"]
    assert isinstance(sock.sent[1]["server_time_ms"], int)
    assert sock.sent[3]["code"] == "invalid_message"
    assert sock.sent[5]["message"] == "Frame is not JSON."
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_missed_pongs_close_the_socket_and_pong_resets(rig) -> None:
    store, _b, router = rig
    sock = FakeSocket()
    session = ClientSession(sock, store, ping_interval_s=0.01)
    task = asyncio.create_task(session.run(router))
    await sock.wait_for(2)
    assert sock.sent[1]["type"] == "ping"
    sock.inbox.put_nowait({"type": "pong"})
    await asyncio.sleep(0.005)
    assert session.missed_pongs == 0
    await asyncio.wait_for(task, timeout=1)  # two unanswered pings -> closed
    assert sock.closed and session.missed_pongs >= 2


async def test_overflow_clears_queue_and_resends_snapshot(rig) -> None:
    store, b, _router = rig
    sock = FakeSocket()
    session = ClientSession(sock, store, ping_interval_s=60, queue_limit=4)
    # No sender running: fill the queue by hand.
    _snapshot, unsub = store.subscribe(session.on_commit)
    session.version = store.state.version
    from illyhub_hub.commands import Ack

    session.on_ack(Ack(correlation_id="c-early", ok=True, action="play", state_version=1))
    for v in range(10):
        b.sonos.sim_volume(PATIO, v)
    unsub()
    assert session.overflows >= 1
    items = list(session.queue)
    # After an overflow the queue restarts from a snapshot and the deltas chain from it, and the
    # session's version tracks what was actually queued (not the older commit's version).
    assert items[0]["type"] == "snapshot"
    version = items[0]["version"]
    for msg in items[1:]:
        assert msg["type"] == "delta" and msg["from_version"] == version
        version = msg["to_version"]
    assert version == store.state.version == session.version
    # Acks live in their own queue and survived the overflow.
    assert [a["correlation_id"] for a in session.acks] == ["c-early"]


async def test_twenty_clients_see_the_same_version_sequence(rig) -> None:
    store, b, router = rig
    socks = [FakeSocket() for _ in range(20)]
    tasks = [
        asyncio.create_task(ClientSession(s, store, ping_interval_s=60).run(router)) for s in socks
    ]
    for s in socks:
        await s.wait_for(1)
    for v in range(5):
        b.sonos.sim_volume(PATIO, 10 + v)
    for s in socks:
        await s.wait_for(6)
    sequences = {
        tuple((m["from_version"], m["to_version"]) for m in s.sent if m["type"] == "delta")
        for s in socks
    }
    assert len(sequences) == 1 and len(next(iter(sequences))) == 5
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_receiver_disconnect_ends_session_cleanly(rig) -> None:
    store, _b, router = rig

    class Disconnecting(FakeSocket):
        async def receive_text(self) -> str:
            await asyncio.sleep(0.005)
            raise WebSocketDisconnect()

    class WebSocketDisconnect(Exception):  # noqa: N818 - mirrors starlette's class name
        pass

    sock = Disconnecting()
    await asyncio.wait_for(ClientSession(sock, store, ping_interval_s=60).run(router), 1)
    assert sock.closed and sock.sent[0]["type"] == "snapshot"


async def test_on_commit_uses_state_when_store_moved_on(rig) -> None:
    store, b, _router = rig
    sock = FakeSocket()
    session = ClientSession(sock, store, ping_interval_s=60)
    session.version = store.state.version
    old_state = store.state
    b.sonos.sim_volume(PATIO, 1)
    b.sonos.sim_volume(PATIO, 2)
    session.on_commit(
        old_state.model_copy(update={"version": old_state.version + 1}), [f"players.{PATIO}"]
    )
    msg = session.queue.popleft()
    assert msg["to_version"] == old_state.version + 1


async def test_acks_are_delivered_before_backlog_and_survive_overflow(rig) -> None:
    store, b, router = rig
    sock = FakeSocket()
    session = ClientSession(sock, store, ping_interval_s=60, queue_limit=3)
    task = asyncio.create_task(session.run(router))
    await sock.wait_for(1)
    # Burst 10 commits with the sender starved (no yield), then a command ack.
    for v in range(10):
        b.sonos.sim_volume(PATIO, 20 + v)
    ack = await router.transport(KITCHEN, "play")
    await sock.wait_for(3)
    kinds = [m["type"] for m in sock.sent[1:]]
    assert "ack" in kinds and "snapshot" in kinds  # overflow -> snapshot; ack still arrived
    assert next(m for m in sock.sent if m["type"] == "ack")["correlation_id"] == ack.correlation_id
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
