"""Rate-limit high-frequency device writes (volume drags, scrubbing): latest value wins.

A :class:`Coalescer` runs the first write for a key immediately, then holds subsequent writes
for that key until ``min_interval_s`` has passed since the last device write, at which point
only the most recent one is executed. This caps device traffic at roughly ``1/min_interval_s``
writes per second per key without ever dropping the final value.

The clock and sleep are injectable so tests can drive it deterministically.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .logsetup import get_logger

log = get_logger("coalesce")

Write = Callable[[], Awaitable[Any]]


class Coalescer:
    def __init__(
        self,
        min_interval_s: float = 0.1,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._last_write: dict[str, float] = {}
        self._pending: dict[str, Write] = {}
        self._flushers: dict[str, asyncio.Task[None]] = {}
        self._draining = False
        self.writes = 0  # device writes performed; tests read this

    async def submit(self, key: str, write: Write) -> bool:
        """Run ``write`` now if the key is idle, else queue it (replacing any queued write).

        Returns True when the write ran synchronously, False when it was deferred. An immediate
        write propagates its exception to the caller (so the command can fail its ack); a
        deferred write that fails is logged, since nobody is waiting on it any more.
        """
        now = self._clock()
        last = self._last_write.get(key)
        if key not in self._flushers and (last is None or now - last >= self.min_interval_s):
            self._last_write[key] = now
            self.writes += 1
            await write()
            return True
        self._pending[key] = write
        if key not in self._flushers:
            since_last = now - last if last is not None else 0.0  # ``last`` may be 0.0
            delay = max(0.0, self.min_interval_s - since_last)
            self._flushers[key] = asyncio.create_task(self._flush_later(key, delay))
        return False

    async def _flush_later(self, key: str, delay: float) -> None:
        try:
            await self._sleep(delay)
            write = self._pending.pop(key, None)
            if write is not None:
                await self._run(key, write)
        finally:
            if self._flushers.get(key) is asyncio.current_task():
                self._flushers.pop(key, None)
            if key in self._pending and not self._draining and key not in self._flushers:
                # a write landed while we were running; go again
                self._flushers[key] = asyncio.create_task(
                    self._flush_later(key, self.min_interval_s)
                )

    async def _run(self, key: str, write: Write) -> None:
        self._last_write[key] = self._clock()
        self.writes += 1
        try:
            await write()
        except Exception:  # noqa: BLE001 - a deferred write must not kill the flusher
            log.exception("coalesced write failed", extra={"extra": {"key": key}})

    async def drain(self) -> None:
        """Flush everything pending now (used at shutdown and by tests). Leaves no flushers."""
        self._draining = True
        try:
            for key in list(self._flushers):
                task = self._flushers.pop(key)
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            for key, write in list(self._pending.items()):
                self._pending.pop(key, None)
                await self._run(key, write)
        finally:
            self._draining = False

    @property
    def active_flushers(self) -> int:
        return len(self._flushers)

    @property
    def pending_keys(self) -> set[str]:
        return set(self._pending)
