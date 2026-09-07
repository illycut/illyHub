from __future__ import annotations

import asyncio
import random

import pytest

from illyhub_hub.adapters.base import Backoff, BaseAdapter
from illyhub_hub.state import StateStore
from illyhub_hub.tasks import spawn, stop_task


def test_backoff_grows_caps_and_clamps_attempts(monkeypatch) -> None:
    monkeypatch.setattr(random, "uniform", lambda a, b: 0.0)
    b = Backoff(base=1.0, cap=8.0, jitter=0.2)
    assert [b.next() for _ in range(5)] == [1.0, 2.0, 4.0, 8.0, 8.0]
    for _ in range(100):
        b.next()
    assert b.attempt == Backoff.MAX_ATTEMPT
    assert b.next() == 8.0  # 2**20 does not blow up the delay
    b.reset()
    assert b.attempt == 0 and b.next() == 1.0


def test_backoff_jitter_stays_inside_cap(monkeypatch) -> None:
    monkeypatch.setattr(random, "uniform", lambda a, b: b)  # maximum jitter
    b = Backoff(base=100.0, cap=10.0, jitter=0.25)
    delay = b.next()
    assert delay == pytest.approx(7.5) and delay <= b.cap


class Stub(BaseAdapter):
    name = "stub"

    async def connect(self) -> None:
        self._mark_connected()
        self._set_status("connected")

    async def disconnect(self) -> None:
        await self._cancel_tasks()
        self._set_status("disconnected")


async def test_status_reads_through_to_store_and_since_agrees(store: StateStore) -> None:
    a = Stub(store)
    assert a.status().state == "disconnected"
    await a.connect()
    assert a.status() == store.state.connections["stub"]
    assert a.status().since == store.state.connections["stub"].since


async def test_held_at_least_gates_backoff_reset(store: StateStore) -> None:
    a = Stub(store)
    assert a._held_at_least(0) is False  # never connected
    await a.connect()
    assert a._held_at_least(0) is True and a._held_at_least(3600) is False


async def test_emit_handles_sync_async_and_bad_callbacks(store: StateStore) -> None:
    a = Stub(store)
    got: list[str] = []
    hit = asyncio.Event()

    async def async_cb(event):
        got.append(f"async:{event.kind}")
        hit.set()

    def bad(_event):
        raise RuntimeError("bad listener")

    unsub = a.on_event(lambda e: got.append(e.kind))
    a.on_event(bad)
    a.on_event(async_cb)
    a._emit("ping", x=1)
    await asyncio.wait_for(hit.wait(), 1)
    assert got == ["ping", "async:ping"]
    unsub()
    unsub()
    a._emit("pong")
    await asyncio.sleep(0)
    assert "pong" not in got
    await a.disconnect()
    assert a._tasks == set()


async def test_spawn_tracks_and_stop_task_swallows_only_own_cancel() -> None:
    tasks: set[asyncio.Task] = set()

    async def sleeper():
        await asyncio.sleep(10)

    t = spawn(sleeper(), tasks)
    assert t in tasks
    await stop_task(t)
    assert tasks == set()
    await stop_task(None)

    async def boom():
        raise ValueError("x")

    t2 = spawn(boom(), tasks)
    await asyncio.sleep(0)
    await stop_task(t2)  # crashed task is logged, not raised


async def test_stop_task_propagates_callers_cancellation() -> None:
    inner = asyncio.create_task(asyncio.sleep(10))

    async def outer():
        await stop_task(inner)

    outer_task = asyncio.create_task(outer())
    await asyncio.sleep(0)
    outer_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer_task


def test_spawn_without_loop_closes_coroutine() -> None:
    async def coro():
        pass  # pragma: no cover

    assert spawn(coro(), set()) is None
