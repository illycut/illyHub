"""WebSocket state push: snapshot on connect, deltas per commit, command acks, heartbeats.

Protocol (all JSON objects with a ``type`` field):

Server → client
- ``snapshot``  ``{version, state}`` full :class:`HubState` dump. Sent on connect, on
  ``resync``, and whenever the client's queue overflowed (the queue is cleared first).
- ``delta``     ``{from_version, to_version, changed}`` where ``changed`` maps a depth-two path
  (``players.heos-1``, ``sync``) to its full new value, or ``null`` when removed. A client that
  sees ``from_version != its version`` should send ``resync``.
- ``ack``       a :class:`~illyhub_hub.commands.Ack` for a command issued over REST. Acks are
  broadcast to every client and ride a separate small queue that a state-queue overflow does
  not clear, so a slow client still learns the outcome of its own command.
- ``ping``      server heartbeat; reply with ``pong``. Two missed pongs close the socket.
- ``pong``      ``{server_time_ms}`` reply to a client ``ping``.
- ``error``     ``{code, message}`` for a malformed client frame (bad JSON or unknown type);
  the connection stays open.

Client → server: ``ping``, ``pong``, ``resync``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Any, Protocol

from .commands import Ack, CommandRouter
from .logsetup import get_logger
from .state import HubState, StateStore

log = get_logger("ws")

QUEUE_LIMIT = 256
ACK_QUEUE_LIMIT = 64
PING_INTERVAL_S = 20.0
MAX_MISSED_PONGS = 2


class Socket(Protocol):
    """The slice of ``starlette.websockets.WebSocket`` the hub uses; tests supply a fake.

    ``receive_text`` is used instead of ``receive_json`` so malformed JSON can be answered with
    an ``error`` frame rather than tearing the connection down.
    """

    async def send_json(self, data: Any) -> None: ...
    async def receive_text(self) -> str: ...
    async def close(self, code: int = 1000) -> None: ...


class DumpCache:
    """JSON dumps of recent HubState versions, shared by every session so a commit that the
    store has already moved past is serialized once, not once per client."""

    def __init__(self, size: int = 8) -> None:
        self.size = size
        self._dumps: OrderedDict[int, dict[str, Any]] = OrderedDict()

    def get(self, state: HubState) -> dict[str, Any]:
        dump = self._dumps.get(state.version)
        if dump is None:
            dump = state.model_dump(mode="json")
            self._dumps[state.version] = dump
            while len(self._dumps) > self.size:
                self._dumps.popitem(last=False)
        return dump


def lookup(dump: dict[str, Any], path: str) -> Any:
    """Value at a depth-two path in a state dump; None when the key was removed."""
    head, _, tail = path.partition(".")
    value = dump.get(head)
    if not tail:
        return value
    return value.get(tail) if isinstance(value, dict) else None


def delta_message(from_version: int, dump: dict[str, Any], changed: list[str]) -> dict[str, Any]:
    return {
        "type": "delta",
        "from_version": from_version,
        "to_version": dump["version"],
        "changed": {path: lookup(dump, path) for path in changed},
    }


class ClientSession:
    """One connected client: bounded outbound queue, heartbeat, and inbound handling."""

    def __init__(
        self,
        socket: Socket,
        store: StateStore,
        *,
        ping_interval_s: float = PING_INTERVAL_S,
        queue_limit: int = QUEUE_LIMIT,
        ack_queue_limit: int = ACK_QUEUE_LIMIT,
        dumps: DumpCache | None = None,
    ) -> None:
        self.socket = socket
        self.store = store
        self.ping_interval_s = ping_interval_s
        self.queue: deque[dict[str, Any]] = deque()  # state + control frames, bounded by hand
        self.queue_limit = queue_limit
        self.acks: deque[dict[str, Any]] = deque(maxlen=ack_queue_limit)  # survives overflow
        self._wake = asyncio.Event()
        self.dumps = dumps or DumpCache()
        self.version = 0
        self.missed_pongs = 0
        self.overflows = 0
        self.closed = asyncio.Event()

    # -- outbound -------------------------------------------------------------------

    def enqueue(self, message: dict[str, Any]) -> bool:
        """Queue a state/control frame. Returns False when the queue overflowed and was
        replaced by a fresh snapshot (the caller must not advance its version past it)."""
        if len(self.queue) >= self.queue_limit:
            self.overflows += 1
            self.queue.clear()
            self.queue.append(self.snapshot_message())
            self._wake.set()
            return False
        self.queue.append(message)
        self._wake.set()
        return True

    def snapshot_message(self) -> dict[str, Any]:
        dump = self.store.snapshot()
        self.version = dump["version"]
        return {"type": "snapshot", "version": dump["version"], "state": dump}

    def on_commit(self, state: HubState, changed: list[str]) -> None:
        dump = self.store.snapshot()
        if dump["version"] != state.version:  # a later commit already superseded this one
            dump = self.dumps.get(state)
        if self.enqueue(delta_message(self.version, dump, changed)):
            self.version = dump["version"]
        # else: enqueue() already reset self.version to the snapshot it queued instead.

    def on_ack(self, ack: Ack) -> None:
        self.acks.append({"type": "ack", **ack.model_dump(mode="json")})
        self._wake.set()

    async def _sender(self) -> None:
        while True:
            while self.acks or self.queue:
                message = self.acks.popleft() if self.acks else self.queue.popleft()
                await self.socket.send_json(message)
            self._wake.clear()
            await self._wake.wait()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.ping_interval_s)
            if self.missed_pongs >= MAX_MISSED_PONGS:
                log.info("client missed pongs; closing")
                self.closed.set()
                return
            self.missed_pongs += 1
            self.enqueue({"type": "ping"})

    # -- inbound --------------------------------------------------------------------

    async def _receiver(self) -> None:
        while True:
            text = await self.socket.receive_text()
            try:
                message = json.loads(text)
            except ValueError:
                self.enqueue(
                    {"type": "error", "code": "invalid_message", "message": "Frame is not JSON."}
                )
                continue
            self.handle(message)

    def handle(self, message: Any) -> None:
        kind = message.get("type") if isinstance(message, dict) else None
        if kind == "pong":
            self.missed_pongs = 0
        elif kind == "ping":
            self.missed_pongs = 0
            self.enqueue({"type": "pong", "server_time_ms": int(time.time() * 1000)})
        elif kind == "resync":
            self.enqueue(self.snapshot_message())
        else:
            self.enqueue(
                {"type": "error", "code": "invalid_message", "message": f"Unknown type {kind!r}."}
            )

    # -- lifecycle ------------------------------------------------------------------

    async def run(self, router: CommandRouter | None = None) -> None:
        """Serve the client until it disconnects or misses its heartbeats."""
        snapshot, unsubscribe = self.store.subscribe(self.on_commit)
        self.version = snapshot["version"]
        self.queue.append({"type": "snapshot", "version": snapshot["version"], "state": snapshot})
        self._wake.set()
        unsub_ack: Callable[[], None] = router.on_ack(self.on_ack) if router else lambda: None
        tasks = [
            asyncio.create_task(self._sender(), name="ws-sender"),
            asyncio.create_task(self._receiver(), name="ws-receiver"),
            asyncio.create_task(self._heartbeat(), name="ws-heartbeat"),
            asyncio.create_task(self.closed.wait(), name="ws-closed"),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception() if not t.cancelled() else None
                if exc is not None and not _is_disconnect(exc):
                    log.debug("ws task ended", extra={"extra": {"error": repr(exc)}})
        finally:
            unsubscribe()
            unsub_ack()
            # Cancel directly (not stop_task): this path also runs when *we* are being
            # cancelled, and the socket must still be closed.
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await self.socket.close()
            except Exception:  # noqa: BLE001 - peer may already be gone
                pass


def _is_disconnect(exc: BaseException) -> bool:
    name = type(exc).__name__
    return name in ("WebSocketDisconnect", "ConnectionClosed", "ConnectionClosedOK")
