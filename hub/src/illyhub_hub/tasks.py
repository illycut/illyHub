"""Fire-and-forget task tracking shared by the StateStore and adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from .logsetup import get_logger

log = get_logger("tasks")


def spawn(
    coro: Coroutine[Any, Any, Any], tasks: set[asyncio.Task[Any]]
) -> asyncio.Task[Any] | None:
    """Schedule ``coro`` on the running loop and track it in ``tasks``.

    Returns None (and closes the coroutine) when no loop is running, e.g. when the store is
    mutated during synchronous wiring before uvicorn starts the loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        log.debug("no running loop; dropped background coroutine")
        return None
    task = loop.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


async def stop_task(task: asyncio.Task[Any] | None) -> None:
    """Cancel ``task`` and await it, swallowing only the cancellation we caused.

    If the *caller* is itself being cancelled, the CancelledError propagates.
    """
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
    except Exception:  # noqa: BLE001 - a crashed task must not break shutdown
        log.debug("background task raised during shutdown", exc_info=True)


async def cancel_all(tasks: set[asyncio.Task[Any]]) -> None:
    for task in list(tasks):
        await stop_task(task)
    tasks.clear()
