from __future__ import annotations

import asyncio

import pytest

from illyhub_hub.coalesce import Coalescer


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s
        await asyncio.sleep(0)


async def test_first_write_is_immediate_and_burst_collapses_to_latest() -> None:
    clock = FakeClock()
    c = Coalescer(0.1, clock=clock, sleep=clock.sleep)
    written: list[int] = []

    async def w(v: int) -> None:
        written.append(v)

    assert await c.submit("vol:a", lambda: w(1)) is True
    for v in range(2, 30):  # a 28-event slider drag inside the same 100 ms window
        clock.t += 0.001
        assert await c.submit("vol:a", lambda v=v: w(v)) is False
    assert written == [1] and c.pending_keys == {"vol:a"}
    for _ in range(10):
        await asyncio.sleep(0)
    assert written == [1, 29]  # only the latest deferred value reached the device
    assert c.writes == 2 and not c.pending_keys


async def test_rate_is_capped_per_key_and_keys_are_independent() -> None:
    clock = FakeClock()
    c = Coalescer(0.1, clock=clock, sleep=clock.sleep)
    written: list[tuple[str, int]] = []

    async def w(k: str, v: int) -> None:
        written.append((k, v))

    await c.submit("a", lambda: w("a", 1))
    await c.submit("b", lambda: w("b", 1))  # different key: immediate
    clock.t += 0.05
    await c.submit("a", lambda: w("a", 2))  # deferred, 50 ms remaining
    assert written == [("a", 1), ("b", 1)]
    for _ in range(10):
        await asyncio.sleep(0)
    assert written[-1] == ("a", 2) and clock.sleeps[-1] == pytest.approx(0.05)
    clock.t += 0.2
    assert await c.submit("a", lambda: w("a", 3)) is True  # window elapsed: immediate again


async def test_write_landing_during_flush_reschedules_and_errors_are_contained() -> None:
    clock = FakeClock()
    c = Coalescer(0.1, clock=clock, sleep=clock.sleep)
    written: list[int] = []
    started = asyncio.Event()

    async def slow(v: int) -> None:
        started.set()
        await asyncio.sleep(0.01)
        written.append(v)

    async def boom() -> None:
        raise RuntimeError("device said no")

    with pytest.raises(RuntimeError):
        await c.submit("k", boom)  # immediate write fails: the caller hears about it
    clock.t += 0.001
    await c.submit("k", lambda: slow(2))
    await started.wait()
    await c.submit("k", lambda: slow(3))  # lands while 2 is being written
    await asyncio.sleep(0.05)
    for _ in range(20):
        await asyncio.sleep(0)
    assert written == [2, 3]


async def test_drain_flushes_pending_now() -> None:
    clock = FakeClock()
    never = asyncio.Event()

    async def block(_s: float) -> None:
        await never.wait()

    c = Coalescer(0.1, clock=clock, sleep=block)
    written: list[int] = []

    async def w(v: int) -> None:
        written.append(v)

    await c.submit("k", lambda: w(1))
    await c.submit("k", lambda: w(2))
    assert written == [1]
    await c.drain()
    assert written == [1, 2] and not c.pending_keys


async def test_drain_leaves_no_flushers_or_tasks() -> None:
    c = Coalescer(0.5)
    written: list[int] = []

    async def w(v: int) -> None:
        written.append(v)

    before = asyncio.all_tasks()
    await c.submit("k", lambda: w(1))
    await c.submit("k", lambda: w(2))
    await asyncio.sleep(0)  # let the flusher task start its sleep
    await c.drain()
    await asyncio.sleep(0)
    assert written == [1, 2] and c.active_flushers == 0 and not c.pending_keys
    assert asyncio.all_tasks() - before == set()
