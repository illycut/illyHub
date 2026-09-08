"""Adapter interfaces shared by the real protocol adapters and the fakes.

Phase 0 covers connection lifecycle, status, and event fan-out. Phase 1 adds the command
surface: :class:`PlaybackAdapter` for the two music ecosystems (transport, seek, volume, mute,
grouping) and :class:`DenonAdapter.set_power` for amplifier zones. Adapters raise
:class:`UnsupportedCommand` for anything their protocol cannot do (HEOS has no seek).
"""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from pydantic import BaseModel, Field

from ..art import ArtHelper
from ..content import ContentRef
from ..logsetup import get_logger
from ..state import ConnectionStatus, ConnState, StateStore
from ..tasks import cancel_all, spawn


class AdapterEvent(BaseModel):
    """Normalized event emitted by an adapter after it has updated the StateStore."""

    kind: str
    adapter: str
    payload: dict[str, Any] = Field(default_factory=dict)


EventCallback = Callable[[AdapterEvent], Awaitable[None] | None]


class Backoff:
    """Exponential backoff, capped, with jitter applied *inside* the cap.

    ``next()`` returns ``min(cap, base * 2**attempt) * (1 - U(0, jitter))``. The attempt counter
    is clamped so ``2**attempt`` cannot overflow on a long outage.
    """

    MAX_ATTEMPT = 20

    def __init__(self, base: float = 1.0, cap: float = 60.0, jitter: float = 0.2) -> None:
        self.base, self.cap, self.jitter = base, cap, jitter
        self.attempt = 0

    def next(self) -> float:
        delay = min(self.cap, self.base * (2**self.attempt))
        self.attempt = min(self.attempt + 1, self.MAX_ATTEMPT)
        return delay * (1 - random.uniform(0, self.jitter))

    def reset(self) -> None:
        self.attempt = 0


class BaseAdapter(ABC):
    """Common lifecycle: connect/disconnect, connection status, event subscription.

    ``status()`` reads through to the store so ``since`` timestamps agree everywhere.
    Background work goes through :meth:`_spawn` so :meth:`_cancel_tasks` can stop it.
    """

    name: str = "adapter"

    def __init__(self, store: StateStore, *, art: ArtHelper | None = None) -> None:
        self.store = store
        self.art = art or ArtHelper()
        self.log = get_logger(f"adapter.{self.name}")
        self._callbacks: list[EventCallback] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._connected_at: float | None = None

    # -- lifecycle ------------------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    def status(self) -> ConnectionStatus:
        return self.store.state.connections.get(self.name, ConnectionStatus(state="disconnected"))

    def _set_status(self, state: ConnState, error: str | None = None) -> None:
        self.store.set_connection(self.name, state, error)

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any] | None:
        return spawn(coro, self._tasks)

    async def _cancel_tasks(self) -> None:
        await cancel_all(self._tasks)

    # -- backoff bookkeeping --------------------------------------------------------

    def _mark_connected(self) -> None:
        self._connected_at = asyncio.get_running_loop().time()

    def _held_at_least(self, seconds: float) -> bool:
        """True when the last connection stayed up for ``seconds``; used to gate backoff reset."""
        if self._connected_at is None:
            return False
        return asyncio.get_running_loop().time() - self._connected_at >= seconds

    # -- events ---------------------------------------------------------------------

    def on_event(self, callback: EventCallback) -> Callable[[], None]:
        self._callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return unsubscribe

    def _emit(self, kind: str, **payload: Any) -> None:
        event = AdapterEvent(kind=kind, adapter=self.name, payload=payload)
        for cb in list(self._callbacks):
            try:
                result = cb(event)
            except Exception:  # noqa: BLE001 - one bad listener must not break the adapter
                self.log.exception("event callback failed", extra={"extra": {"kind": kind}})
                continue
            if asyncio.iscoroutine(result):
                self._spawn(result)


class UnsupportedCommandError(Exception):
    """The protocol has no way to perform this command (e.g. seek on the HEOS CLI)."""


class ContentUnavailableError(Exception):
    """This ecosystem cannot play the content: the service is not linked on that side."""


class ServiceAuthError(ContentUnavailableError):
    """The ecosystem is linked to the service but its session is broken (SMAPI auth fault,
    expired token). Distinct from "not linked": the fix is re-signing in the vendor app, and the
    hub must not report the account as empty."""


class PlayableTrack(BaseModel):
    """One track resolved from a service, in the shape both ecosystems need to build a queue
    entry. Ids are the service's canonical ids (Tidal track/album ids are integers-as-strings)."""

    service: str
    track_id: str
    title: str
    artist: str | None = None
    album: str | None = None
    album_id: str | None = None
    playlist_id: str | None = None
    duration_ms: int | None = None
    art_url: str | None = None


class VendorStation(BaseModel):
    """A radio station as one ecosystem sees it (Phase 5, Pandora). ``ids`` holds whatever that
    vendor needs to start it again (HEOS: ``sid``/``cid``/``mid``; Sonos: the SMAPI item ``id``).
    The hub merges stations across vendors by normalised ``name`` (docs/spikes/pandora-refs.md)."""

    vendor: str
    service: str = "pandora"
    name: str
    ids: dict[str, str] = Field(default_factory=dict)
    art_url: str | None = None
    subtitle: str | None = None


class PrimedQueue(BaseModel):
    """What an adapter loaded at the start position of a primed queue, for the sync engine's
    verification step. Either field may be unknown; ``title`` is the fallback comparison."""

    track_id: str | None = None
    title: str | None = None


class PlaybackAdapter(BaseAdapter, ABC):
    """Command surface for a music ecosystem. Player ids are hub ids (``heos-1``, ``sonos-…``).

    Transport commands target the *coordinator* of a side; the router resolves that. Volume and
    mute target individual players.

    Grouping contract: ``set_group`` receives the **desired** full member list, coordinator
    first, and the adapter reconciles it against the current topology (players not listed leave,
    new ones join). A single-element list removes that player from whatever group it is in.
    ``dissolve`` takes a side apart; vendors differ (HEOS needs the leader's id, Sonos has every
    non-coordinator ``unjoin``), which is why the router does not express it via ``set_group``.
    """

    @abstractmethod
    async def play(self, player_id: str) -> None: ...

    @abstractmethod
    async def pause(self, player_id: str) -> None: ...

    @abstractmethod
    async def stop(self, player_id: str) -> None: ...

    @abstractmethod
    async def next(self, player_id: str) -> None: ...

    @abstractmethod
    async def previous(self, player_id: str) -> None: ...

    @abstractmethod
    async def seek(self, player_id: str, position_ms: int) -> None: ...

    @abstractmethod
    async def set_volume(self, player_id: str, level: int) -> None: ...

    @abstractmethod
    async def set_mute(self, player_id: str, muted: bool) -> None: ...

    @abstractmethod
    async def set_group(self, member_ids: list[str]) -> None: ...

    @abstractmethod
    async def dissolve(self, coordinator_id: str, member_ids: list[str]) -> None: ...

    def service_linked(self, service: str) -> bool:
        """Whether this ecosystem has an account for ``service`` (Tidal, Pandora …). Drives the
        per-side availability flags. Default: unknown → False; real adapters override."""
        return False

    @abstractmethod
    async def play_content(
        self, player_id: str, ref: ContentRef, tracks: list[PlayableTrack], start_index: int = 0
    ) -> None:
        """Replace the coordinator's queue with ``tracks`` (resolved from ``ref`` by the service
        layer) and start at ``start_index``. Raises :class:`ContentUnavailableError` when the
        ecosystem has no account for the service (Phase 3, PRD review §2.1)."""

    async def prime_content(
        self, player_id: str, ref: ContentRef, tracks: list[PlayableTrack], start_index: int = 0
    ) -> PrimedQueue:
        """Sync Play step 2: load ``tracks`` into the coordinator's queue positioned at
        ``start_index`` **without starting playback**, and describe what sits at that position.
        Default: adapters that cannot load-without-playing raise ``UnsupportedCommandError``."""
        raise UnsupportedCommandError(f"{self.name} cannot prime a queue")

    async def start_primed(self, player_id: str, start_index: int = 0) -> None:
        """Sync Play step 4: start the queue primed by :meth:`prime_content` from position 0 of
        ``start_index``. Default: plain ``play``."""
        await self.play(player_id)

    async def list_stations(self, service: str) -> list[VendorStation]:
        """Phase 5: the user's radio stations for ``service`` as this ecosystem lists them
        (PRD PAN-1/PAN-2). Raises :class:`ContentUnavailableError` when the service is not
        linked on this side. Default: nothing."""
        raise ContentUnavailableError(f"{service} stations are not available on {self.name}")

    async def play_station(self, player_id: str, ref: ContentRef, station: VendorStation) -> None:
        """Phase 5: start a radio station natively on the coordinator. ``ref`` is the hub's
        vendor-neutral station ref, stamped on now-playing so clients know which station plays;
        ``station`` carries this vendor's own ids. Default: unsupported."""
        raise ContentUnavailableError(f"{self.name} cannot play {ref.service} stations")

    async def snapshot_queue(self, player_id: str) -> Any | None:
        """Capture the coordinator's queue and transport so a failed Sync Play start can put the
        room back (Sonos: SoCo ``Snapshot``). Default: nothing to restore."""
        return None

    async def restore_queue(self, player_id: str, snapshot: Any) -> None:
        """Undo :meth:`snapshot_queue`. Default: no-op."""


class HeosAdapter(PlaybackAdapter, ABC):
    name = "heos"


class SonosAdapter(PlaybackAdapter, ABC):
    name = "sonos"


class DenonAdapter(BaseAdapter, ABC):
    name = "denon"

    @abstractmethod
    async def set_power(self, zone_id: str, on: bool) -> None:
        """Zone power (ZON-1). ``zone_id`` may be a bare key or the namespaced id."""
