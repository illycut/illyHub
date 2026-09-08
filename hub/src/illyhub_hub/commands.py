"""Normalized commands, target resolution, and dispatch to protocol adapters.

A **target** is a player id, a side id, or ``"all"``. Transport and seek commands resolve to
sides and act on each side's coordinator; volume and mute resolve to players. Every command
produces exactly one terminal :class:`Ack` per correlation id, carrying the state version the
router observed after it ran (a lower bound for the effect, since device writes may still be
in flight or coalesced). Multi-target commands apply to every target they can and report the
ones that failed in ``partial``; ``ok`` is false only when nothing succeeded. Failures use the
:class:`ErrorEnvelope` with a code from :data:`ERROR_CATALOGUE` and a message written as a plain
statement of what did not happen.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .adapters.base import (
    ContentUnavailableError,
    DenonAdapter,
    PlayableTrack,
    PlaybackAdapter,
    ServiceAuthError,
    UnsupportedCommandError,
    VendorStation,
)
from .coalesce import Coalescer
from .config import Vendor
from .content import Availability, ContentRef
from .logsetup import correlation_id, get_logger
from .messages import (
    NOT_AVAILABLE_NOT_LINKED,
    NOT_AVAILABLE_UNSUPPORTED,
    PANDORA_CONCURRENT_MSG,
    PLAY_MODE_LOCKED,
    QUEUE_UNAVAILABLE,
    STATION_AUTH_FAULT,
    STATION_NOT_IN_ACCOUNT,
    UNSUPPORTED_ON_VENDOR,
    vendor_app,
)
from .state import (
    HubState,
    Player,
    PlayMode,
    QueueEntry,
    QueueState,
    Side,
    StateStore,
    Zone,
    service_label,
    vendor_label,
    zone_for_player,
)

log = get_logger("commands")

ErrorCode = Literal[
    "unknown_target",
    "unsupported_action",
    "not_seekable",
    "adapter_disconnected",
    "device_offline",
    "invalid_argument",
    "vendor_error",
    "needs_link",
    "not_available_on_side",
    "unsupported_content",
    "sync_mismatch",
    "sync_idle",
    "sync_stopped",
    "bridge_unavailable",
    "sync_active",
    "bridge_active",
]

ERROR_CATALOGUE: dict[str, tuple[int, str]] = {
    # code: (HTTP status, meaning)
    "unknown_target": (404, "No player, side, zone, or 'all' matches the target."),
    "unsupported_action": (409, "The protocol for this device cannot perform the action."),
    "not_seekable": (409, "The current source (e.g. a radio station) does not support seek."),
    "adapter_disconnected": (503, "The hub has no live connection to that ecosystem right now."),
    "device_offline": (409, "The target device is known but not reachable."),
    "invalid_argument": (
        400,
        "A parameter is out of range or malformed (e.g. linked volume "
        "with neither level nor delta, or a group across vendors).",
    ),
    "vendor_error": (502, "The device rejected the command or the hub hit an unexpected error."),
    "needs_link": (409, "The streaming service is not linked to the hub yet."),
    "not_available_on_side": (
        409,
        "The streaming service is not linked inside that ecosystem (vendor app), so that side "
        "cannot play the content.",
    ),
    "unsupported_content": (409, "Only Tidal albums, playlists and tracks can Sync Play."),
    "sync_mismatch": (409, "The two sides did not load the same first track; nothing started."),
    "sync_idle": (409, "There is no Sync Play session to stop or retry."),
    "sync_stopped": (409, "Sync Play was stopped before it finished starting."),
    "sync_active": (409, "A Sync Play session is live; stop it before Pandora Sync."),
    "bridge_active": (409, "Pandora Sync is on; stop it before Sync Play."),
    "bridge_unavailable": (
        503,
        "The hub Mac's AirPlay bridge is off, Music is not responding, or Automation permission "
        "is missing (experimental Pandora Sync).",
    ),
}

TransportAction = Literal["play", "pause", "toggle", "stop", "next", "prev"]
SKIP_MS = 15_000


class CommandError(Exception):
    def __init__(self, code: ErrorCode, message: str, target: str | None = None) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.target = target


class ErrorEnvelope(BaseModel):
    code: ErrorCode
    message: str
    target: str | None = None
    correlation_id: str


class PartialFailure(BaseModel):
    target: str
    code: ErrorCode
    message: str


WarningCode = Literal["pandora_concurrent"]


class AckWarning(BaseModel):
    """A non-fatal note about a command that still succeeded (e.g. a second Pandora stream)."""

    code: WarningCode
    message: str
    target: str | None = None


class Ack(BaseModel):
    correlation_id: str
    ok: bool
    action: str
    target: str | None = None
    state_version: int
    error: ErrorEnvelope | None = None
    partial: list[PartialFailure] = Field(default_factory=list)
    applied: list[str] = Field(
        default_factory=list, description="targets (side or player ids) the command reached"
    )
    session_id: str | None = Field(default=None, description="Sync Play session id (sync_* acks)")
    warnings: list[AckWarning] = Field(
        default_factory=list, description="non-fatal notes, e.g. pandora_concurrent"
    )
    resolved: dict[str, str] = Field(
        default_factory=dict,
        description="transport acks: side id -> the action actually sent (toggle resolved)",
    )
    latency_ms: float = Field(default=0.0, description="hub-side time from request to ack")


TRANSPORT_KINDS = ("transport",)


class LatencyTracker:
    """Rolling average of adapter command round-trips per vendor and kind (last ``window`` calls
    each). Kinds: ``transport`` (play/pause/next/prev/stop), ``seek``, ``volume``, ``prime``.

    The sync engine's start ordering (PRD §7 step 3) reads the **transport** window only: a
    500 ms queue prime says nothing about how fast ``play`` lands.
    """

    def __init__(self, window: int = 10) -> None:
        self.window = window
        self._samples: dict[tuple[str, str], deque[float]] = {}

    def record(self, vendor: str, ms: float, kind: str = "transport") -> None:
        self._samples.setdefault((vendor, kind), deque(maxlen=self.window)).append(ms)

    def average(
        self, vendor: str, default: float = 0.0, kinds: tuple[str, ...] = TRANSPORT_KINDS
    ) -> float:
        samples = [
            v
            for (ven, kind), dq in self._samples.items()
            if ven == vendor and kind in kinds
            for v in dq
        ]
        if not samples:
            return default
        return sum(samples) / len(samples)

    def count(self, vendor: str, kinds: tuple[str, ...] = TRANSPORT_KINDS) -> int:
        return sum(
            len(dq) for (ven, kind), dq in self._samples.items() if ven == vendor and kind in kinds
        )


AckCallback = Callable[[Ack], Awaitable[None] | None]


def _name(state: HubState, target: str) -> str:
    if target in state.players:
        return state.players[target].name
    if target in state.sides:
        return state.sides[target].name
    if target in state.zones:
        return state.zones[target].name
    return target


def resolve_sides(state: HubState, target: str) -> list[Side]:
    if target == "all":
        return list(state.sides.values())
    if target in state.sides:
        return [state.sides[target]]
    if target in state.players:
        player = state.players[target]
        for side in state.sides.values():
            if player.id in side.member_ids:
                return [side]
    raise CommandError("unknown_target", f"Nothing is called {target!r}.", target)


def resolve_players(state: HubState, target: str) -> list[Player]:
    if target == "all":
        return list(state.players.values())
    if target in state.players:
        return [state.players[target]]
    if target in state.sides:
        return [state.players[m] for m in state.sides[target].member_ids if m in state.players]
    raise CommandError("unknown_target", f"Nothing is called {target!r}.", target)


def linked_volumes(
    current: dict[str, int],
    ratios: dict[str, float],
    *,
    level: int | None,
    delta: int | None,
) -> tuple[dict[str, int], dict[str, float]]:
    """Levels for a master move that preserve each player's ratio to the loudest player.

    ``ratios`` are the router's remembered per-player ratios (floats), so a quiet player that
    the integer clamp pinned at 1 keeps its true ratio for the next move instead of collapsing.
    Returns ``(new_levels, new_ratios)``. Non-zero players never drop below 1 while the master
    is above 0; when every player is at 0 a positive move raises them all together.
    """
    if not current:
        return {}, dict(ratios)
    master = max(current.values())
    if level is None:
        if delta is None:
            raise CommandError("invalid_argument", "Linked volume needs a level or a delta.")
        new_master = master + delta
    else:
        new_master = level
    new_master = max(0, min(100, new_master))
    new_ratios = dict(ratios)
    if master == 0:
        for pid in current:
            new_ratios[pid] = 1.0
        return dict.fromkeys(current, new_master), new_ratios
    levels: dict[str, int] = {}
    for pid, vol in current.items():
        ratio = new_ratios.get(pid)
        if ratio is None or vol == 0:
            ratio = vol / master
            new_ratios[pid] = ratio
        target = math.floor(ratio * new_master + 0.5)
        if ratio > 0 and new_master > 0:
            target = max(1, target)
        levels[pid] = max(0, min(100, target))
    return levels, new_ratios


class _Outcomes:
    """Per-target results of one command, for partial-success acks."""

    def __init__(self) -> None:
        self.attempted = 0
        self.failures: list[CommandError] = []
        self.applied: list[str] = []
        self.resolved: dict[str, str] = {}  # transport: side id -> action actually sent
        self.warnings: list[AckWarning] = []

    async def attempt(self, target: str, coro: Awaitable[None]) -> bool:
        self.attempted += 1
        try:
            await coro
            self.applied.append(target)
            return True
        except CommandError as exc:
            exc.target = target  # per-target outcome, not the user's selector ("all")
            self.failures.append(exc)
            return False

    @property
    def all_failed(self) -> bool:
        return self.attempted > 0 and len(self.failures) == self.attempted


Run = Callable[[_Outcomes], Awaitable[None]]


class CommandRouter:
    """Resolves targets against the state model and dispatches to the vendor adapters."""

    def __init__(
        self,
        store: StateStore,
        *,
        playback: dict[str, PlaybackAdapter] | None = None,
        denon: DenonAdapter | None = None,
        coalescer: Coalescer | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.playback: dict[str, PlaybackAdapter] = dict(playback or {})
        self.denon = denon
        self.coalescer = coalescer or Coalescer()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ack_callbacks: list[AckCallback] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pending_seek: dict[str, int] = {}  # side id -> absolute target not yet written
        self._linked_ratios: dict[str, float] = {}
        self._linked_written: dict[str, int] = {}  # last level the linked master wrote
        self.latency = LatencyTracker()
        # Set by the API wiring: whether a Sync Play session is live (play-mode changes are
        # refused while the engine holds both sides to one mode).
        self.sync_active: Callable[[], bool] = lambda: False
        self.queue_max = 200

    # -- wiring ---------------------------------------------------------------------

    def register(self, adapter: PlaybackAdapter | DenonAdapter) -> None:
        if isinstance(adapter, DenonAdapter):
            self.denon = adapter
        else:
            self.playback[adapter.name] = adapter

    def on_ack(self, callback: AckCallback) -> Callable[[], None]:
        self._ack_callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._ack_callbacks:
                self._ack_callbacks.remove(callback)

        return unsubscribe

    async def aclose(self) -> None:
        await self.coalescer.drain()

    # -- public commands ------------------------------------------------------------

    async def transport(self, target: str, action: TransportAction) -> Ack:
        async def run(out: _Outcomes) -> None:
            for side in resolve_sides(self.state, target):
                resolved = action
                if action == "toggle":
                    resolved = "pause" if side.play_state == "play" else "play"
                if await out.attempt(side.id, self._transport_side(side, resolved, target)):
                    out.resolved[side.id] = resolved

        return await self._execute(action, target, run)

    async def _transport_side(self, side: Side, action: str, target: str) -> None:
        adapter = self._adapter_for(side.vendor, target)
        self._require_online(side.coordinator_player_id, target)
        if action == "next" and not side.capabilities.supports_next:
            raise CommandError("unsupported_action", f"{side.name} has no next track.", target)
        if action == "prev" and not side.capabilities.supports_prev:
            raise CommandError("unsupported_action", f"{side.name} has no previous track.", target)
        await self._call(
            lambda: self._transport_on(adapter, side, action),
            action,
            target,
            side.name,
            vendor=side.vendor,
        )

    async def seek(self, target: str, position_ms: int) -> Ack:
        async def run(out: _Outcomes) -> None:
            if position_ms < 0:
                raise CommandError("invalid_argument", "Position must be zero or more.", target)
            for side in resolve_sides(self.state, target):
                await out.attempt(side.id, self._seek_side(side, position_ms, "seek", target))

        return await self._execute("seek", target, run)

    async def skip(self, target: str, delta_ms: int = SKIP_MS) -> Ack:
        async def run(out: _Outcomes) -> None:
            for side in resolve_sides(self.state, target):
                base = self._pending_seek.get(side.id)
                if base is None:
                    base = self._extrapolated_position(side)
                await out.attempt(side.id, self._seek_side(side, base + delta_ms, "skip", target))

        return await self._execute("skip", target, run)

    def _extrapolated_position(self, side: Side) -> int:
        pos = self.state.positions.get(side.id)
        if pos is None:
            return 0
        if side.play_state != "play":
            return pos.position_ms
        elapsed = (self._clock() - pos.reported_at).total_seconds()
        return pos.position_ms + max(0, int(elapsed * 1000))

    async def _seek_side(self, side: Side, position_ms: int, action: str, target: str) -> None:
        adapter = self._adapter_for(side.vendor, target)
        self._require_seekable(side, target)
        np = self.state.now_playing.get(side.id)
        pos = max(0, position_ms)
        if np and np.duration_ms:
            pos = min(pos, np.duration_ms)
        self._pending_seek[side.id] = pos
        coord, sid, name, vendor = side.coordinator_player_id, side.id, side.name, side.vendor

        async def write() -> None:
            try:
                await self._call(
                    lambda: adapter.seek(coord, pos), action, target, name, vendor=vendor
                )
            finally:
                if self._pending_seek.get(sid) == pos:
                    self._pending_seek.pop(sid, None)

        await self.coalescer.submit(f"seek:{sid}", write)

    async def volume(self, target: str, level: int) -> Ack:
        async def run(out: _Outcomes) -> None:
            if not 0 <= level <= 100:
                raise CommandError("invalid_argument", "Volume must be between 0 and 100.", target)
            # An amplifier zone can be a volume target in its own right. A zone that drives an
            # external amp has no player to address it by, so without this its level is
            # unreachable even though the adapter can set it.
            if target in self.state.zones:
                await out.attempt(target, self._set_zone_volume(target, level))
                return
            for player in resolve_players(self.state, target):
                if await out.attempt(player.id, self._set_player_volume(player, level, target)):
                    self._linked_written.pop(player.id, None)  # user moved it: re-learn ratio

        return await self._execute("volume", target, run)

    async def _set_zone_volume(self, zone_id: str, level: int) -> None:
        zone = self.state.zones[zone_id]
        if not zone.supports_volume:
            raise CommandError(
                "unsupported_action",
                f"{zone.name} feeds an external amplifier; set the volume there.",
                zone_id,
            )
        denon = self._require_denon(zone, zone_id)
        await self.coalescer.submit(
            f"volume:{zone_id}",
            lambda: self._call(
                lambda: denon.set_volume(zone_id, level), "volume", zone_id, zone.name
            ),
        )

    async def linked_volume(self, *, level: int | None = None, delta: int | None = None) -> Ack:
        async def run(out: _Outcomes) -> None:
            players = [p for p in self.state.players.values() if p.online]
            if not players:
                raise CommandError("device_offline", "No players are online.", "all")
            current = {p.id: p.volume for p in players}
            # Forget a remembered ratio when the player moved since we last wrote it (vendor app
            # or a direct volume command); keep it when the value is our own clamped output.
            ratios = {
                pid: r
                for pid, r in self._linked_ratios.items()
                if pid in current and self._linked_written.get(pid) == current[pid]
            }
            new_levels, new_ratios = linked_volumes(current, ratios, level=level, delta=delta)
            self._linked_ratios.update(new_ratios)
            for player in players:
                new = new_levels[player.id]
                if new == player.volume:
                    self._linked_written[player.id] = new
                    continue
                if await out.attempt(player.id, self._set_player_volume(player, new, "all")):
                    # Record what the device ended up reporting, not what we asked for. An
                    # amplifier zone quantises the hub's 0-100 onto its own step scale (0-60 by
                    # default, so 61 values): asking for 96 lands on MV58 and reads back 97.
                    # Storing the request would make the next linked call read that one-point
                    # difference as the user having turned the knob and drop the learned ratio.
                    settled = self.state.players.get(player.id)
                    self._linked_written[player.id] = settled.volume if settled else new

        return await self._execute("linked_volume", "all", run)

    async def mute(self, target: str, muted: bool) -> Ack:
        async def run(out: _Outcomes) -> None:
            if target in self.state.zones:
                await out.attempt(target, self._set_zone_mute(target, muted))
                return
            for player in resolve_players(self.state, target):
                await out.attempt(player.id, self._mute_player(player, muted, target))

        return await self._execute("mute", target, run)

    async def _set_zone_mute(self, zone_id: str, muted: bool) -> None:
        zone = self.state.zones[zone_id]
        if not zone.supports_mute:
            raise CommandError(
                "unsupported_action",
                f"{zone.name} feeds an external amplifier; mute it there.",
                zone_id,
            )
        denon = self._require_denon(zone, zone_id)
        await self._call(lambda: denon.set_mute(zone_id, muted), "mute", zone_id, zone.name)

    async def _mute_player(self, player: Player, muted: bool, target: str) -> None:
        self._require_online(player.id, target)
        zone = self._volume_zone(player, target)
        if zone is not None:
            denon = self._require_denon(zone, target)
            zid = zone.id
            await self._call(
                lambda: denon.set_mute(zid, muted),
                "mute",
                target,
                player.name,
                vendor=player.vendor,
            )
            return
        adapter = self._adapter_for(player.vendor, target)
        await self._call(
            lambda: adapter.set_mute(player.id, muted),
            "mute",
            target,
            player.name,
            vendor=player.vendor,
        )

    async def zone_power(self, zone_id: str, on: bool) -> Ack:
        async def run(out: _Outcomes) -> None:
            state = self.state
            if zone_id in state.zones:
                await self._denon_power(state, zone_id, on)
                return
            # Sonos has no power concept: "off" is stop + leave the group; "on" is a no-op.
            for side in resolve_sides(state, zone_id):
                await out.attempt(side.id, self._sonos_power(side, on, zone_id))

        return await self._execute("zone_power", zone_id, run)

    async def _denon_power(self, state: HubState, zone_id: str, on: bool) -> None:
        zone = state.zones[zone_id]
        if self.denon is None or self.denon.status().state != "connected":
            raise CommandError(
                "adapter_disconnected",
                f"{zone.name} didn't change; the amplifier link is down.",
                zone_id,
            )
        if not zone.online:
            raise CommandError("device_offline", f"{zone.name} is not answering.", zone_id)
        denon = self.denon
        await self._call(lambda: denon.set_power(zone_id, on), "zone_power", zone_id, zone.name)

    async def _sonos_power(self, side: Side, on: bool, target: str) -> None:
        if on:
            return
        if side.vendor != "sonos":
            raise CommandError(
                "unsupported_action",
                f"{side.name} has no power control; use its amplifier zone.",
                target,
            )
        adapter = self._adapter_for("sonos", target)
        coord = side.coordinator_player_id
        await self._call(lambda: adapter.stop(coord), "zone_power", target, side.name)
        if len(side.member_ids) > 1:
            await self._call(
                lambda: adapter.dissolve(coord, side.member_ids), "zone_power", target, side.name
            )

    async def group(self, vendor: Vendor, coordinator_id: str, member_ids: list[str]) -> Ack:
        members = [coordinator_id, *[m for m in member_ids if m != coordinator_id]]
        target = coordinator_id

        async def run(_out: _Outcomes) -> None:
            adapter = self._adapter_for(vendor, target)
            for m in members:
                p = self.state.players.get(m)
                if p is None:
                    raise CommandError("unknown_target", f"Nothing is called {m!r}.", m)
                if p.vendor != vendor:
                    raise CommandError(
                        "invalid_argument",
                        f"{p.name} can't join a {vendor_label(vendor)} group; groups stay "
                        "within one system.",
                        m,
                    )
                if not p.capabilities.can_group:
                    raise CommandError("unsupported_action", f"{p.name} can't be grouped.", m)
                self._require_online(m, m)
            await self._call(
                lambda: adapter.set_group(members), "group", target, _name(self.state, target)
            )

        return await self._execute("group", target, run)

    async def ungroup(self, side_id: str) -> Ack:
        async def run(_out: _Outcomes) -> None:
            side = self.state.sides.get(side_id)
            if side is None:
                raise CommandError("unknown_target", f"Nothing is called {side_id!r}.", side_id)
            adapter = self._adapter_for(side.vendor, side_id)
            coord, members = side.coordinator_player_id, side.member_ids
            await self._call(
                lambda: adapter.dissolve(coord, members), "ungroup", side_id, side.name
            )

        return await self._execute("ungroup", side_id, run)

    async def play_content(
        self,
        target: str,
        ref: ContentRef,
        tracks: list[PlayableTrack],
        *,
        start_index: int = 0,
        availability: Availability | None = None,
    ) -> Ack:
        """Replace each target side's queue with ``tracks`` and start at ``start_index``.

        ``availability`` (which ecosystems have the service linked) comes from the service layer;
        a side whose vendor lacks the service fails with ``not_available_on_side`` while the
        others still play. The service layer has already raised ``needs_link`` if the hub itself
        is not linked, so an empty ``tracks`` list here is a content problem, not an auth one.
        """
        avail = availability or self.service_availability(ref.service)

        async def run(out: _Outcomes) -> None:
            if not tracks:
                raise CommandError("invalid_argument", "There is nothing to play.", target)
            if not 0 <= start_index < len(tracks):
                raise CommandError("invalid_argument", "Start index is out of range.", target)
            for side in resolve_sides(self.state, target):
                await out.attempt(
                    side.id, self._play_on_side(side, ref, tracks, start_index, avail, target)
                )

        return await self._execute("play_content", target, run)

    def _side_ladder(
        self, side: Side, service: str, avail: Availability, target: str
    ) -> PlaybackAdapter:
        """The checks every per-side content command runs, in order: service playable on that
        vendor at all (``not_available_on_side``, independent of connection state), adapter
        connected (``adapter_disconnected``), service linked on that vendor
        (``not_available_on_side``), coordinator online (``device_offline``)."""
        if (side.vendor, service) in UNSUPPORTED_ON_VENDOR:
            raise CommandError(
                "not_available_on_side",
                NOT_AVAILABLE_UNSUPPORTED.format(
                    service=service_label(service), vendor=vendor_label(side.vendor)
                ),
                side.id,
            )
        adapter = self._adapter_for(side.vendor, target)
        if not getattr(avail, side.vendor, False):
            raise CommandError(
                "not_available_on_side",
                NOT_AVAILABLE_NOT_LINKED.format(
                    side=side.name,
                    service=service_label(service),
                    vendor_app=vendor_app(side.vendor),
                ),
                side.id,
            )
        self._require_online(side.coordinator_player_id, target)
        return adapter

    async def _play_on_side(
        self,
        side: Side,
        ref: ContentRef,
        tracks: list[PlayableTrack],
        start_index: int,
        avail: Availability,
        target: str,
    ) -> None:
        adapter = self._side_ladder(side, ref.service, avail, target)
        coord = side.coordinator_player_id
        try:
            await adapter.play_content(coord, ref, tracks, start_index)
        except ContentUnavailableError as exc:
            raise CommandError(
                "not_available_on_side",
                NOT_AVAILABLE_NOT_LINKED.format(
                    side=side.name,
                    service=service_label(ref.service),
                    vendor_app=vendor_app(side.vendor),
                ),
                side.id,
            ) from exc
        except CommandError:
            raise
        except Exception as exc:  # noqa: BLE001 - vendor libraries raise their own types
            log.warning(
                "play_content failed",
                extra={"extra": {"target": side.id, "content": ref.key, "error": str(exc)}},
            )
            raise CommandError(
                "vendor_error", f"Play didn't happen on {side.name}.", side.id
            ) from exc

    # -- radio stations (Phase 5) ---------------------------------------------------------

    PANDORA_CONCURRENT_MSG = PANDORA_CONCURRENT_MSG  # copy of record lives in messages.py

    async def play_station(
        self,
        target: str,
        ref: ContentRef,
        stations: Mapping[str, VendorStation],
        *,
        availability: Availability | None = None,
        auth_faults: Mapping[str, str] | None = None,
    ) -> Ack:
        """Start a radio station natively on each target side using that vendor's own ids
        (``stations`` is vendor → :class:`VendorStation`). A side whose vendor lacks the station
        or the service fails with ``not_available_on_side`` while the others start. When another
        room already streams the service, or two target sides start it together, the ack carries
        a ``pandora_concurrent`` warning (PRD §3.5: one stream per account)."""
        avail = availability or self.service_availability(ref.service)

        async def run(out: _Outcomes) -> None:
            sides = resolve_sides(self.state, target)
            target_ids = {s.id for s in sides}
            # Rooms already holding a Pandora stream, judged BEFORE the attempts change state.
            # A paused room still holds its session, so it counts.
            others = [
                s
                for s in self.state.sides.values()
                if s.id not in target_ids and self._holding(s.id, ref.service)
            ]
            for side in sides:
                await out.attempt(
                    side.id,
                    self._station_on_side(
                        side, ref, stations.get(side.vendor), avail, target, auth_faults or {}
                    ),
                )
            if ref.service == "pandora" and out.applied and (others or len(out.applied) > 1):
                applied_vendors = {
                    self.state.sides[a].vendor for a in out.applied if a in self.state.sides
                }
                others.sort(key=lambda s: (s.vendor not in applied_vendors, s.name))
                out.warnings.append(
                    AckWarning(
                        code="pandora_concurrent",
                        message=self.PANDORA_CONCURRENT_MSG,
                        target=others[0].id if others else None,
                    )
                )

        return await self._execute("play_station", target, run)

    def _holding(self, side_id: str, service: str) -> bool:
        side = self.state.sides.get(side_id)
        np = self.state.now_playing.get(side_id)
        return bool(side and np and side.play_state in ("play", "pause") and np.source == service)

    async def _station_on_side(
        self,
        side: Side,
        ref: ContentRef,
        station: VendorStation | None,
        avail: Availability,
        target: str,
        auth_faults: Mapping[str, str],
    ) -> None:
        adapter = self._side_ladder(side, ref.service, avail, target)
        pretty = service_label(ref.service)
        if side.vendor in auth_faults:
            # Linked, but that ecosystem's service session is broken: never say the station is
            # missing from the account when the account could not be read at all.
            raise CommandError(
                "not_available_on_side",
                STATION_AUTH_FAULT.format(
                    side=side.name, service=pretty, vendor_app=vendor_app(side.vendor)
                ),
                side.id,
            )
        if station is None:
            raise CommandError(
                "not_available_on_side",
                STATION_NOT_IN_ACCOUNT.format(
                    vendor=vendor_label(side.vendor), service=pretty, side=side.name
                ),
                side.id,
            )
        try:
            await adapter.play_station(side.coordinator_player_id, ref, station)
        except ServiceAuthError as exc:
            raise CommandError(
                "not_available_on_side",
                STATION_AUTH_FAULT.format(
                    side=side.name, service=pretty, vendor_app=vendor_app(side.vendor)
                ),
                side.id,
            ) from exc
        except ContentUnavailableError as exc:
            raise CommandError(
                "not_available_on_side",
                NOT_AVAILABLE_NOT_LINKED.format(
                    side=side.name, service=pretty, vendor_app=vendor_app(side.vendor)
                ),
                side.id,
            ) from exc
        except CommandError:
            raise
        except Exception as exc:  # noqa: BLE001 - vendor libraries raise their own types
            log.warning(
                "play_station failed",
                extra={"extra": {"target": side.id, "content": ref.key, "error": str(exc)}},
            )
            raise CommandError(
                "vendor_error", f"{station.name} didn't start on {side.name}.", side.id
            ) from exc

    def service_availability(self, service: str) -> Availability:
        """Which ecosystems can play the service, with reasons (``unsupported`` beats
        ``not_linked``)."""
        return Availability.build(
            service,
            {"heos": self._linked("heos", service), "sonos": self._linked("sonos", service)},
        )

    def _linked(self, vendor: str, service: str) -> bool:
        adapter = self.playback.get(vendor)
        if adapter is None or adapter.status().state != "connected":
            return False
        try:
            return bool(adapter.service_linked(service))
        except Exception:  # noqa: BLE001
            return False

    # -- queue and play mode (Phase 8, ai-dev #77 / #79) -----------------------------------

    async def queue(self, side_id: str) -> QueueState:
        """Read a side's native queue from its coordinator and publish it as ``queues.<side>``.
        Raises :class:`CommandError` (unknown_target, adapter_disconnected, unsupported_action)."""
        side = self.state.sides.get(side_id)
        if side is None:
            raise CommandError("unknown_target", f"Nothing is called {side_id!r}.", side_id)
        adapter = self._adapter_for(side.vendor, side_id)
        try:
            entries, total = await adapter.get_queue(side.coordinator_player_id, self.queue_max)
        except UnsupportedCommandError as exc:
            raise CommandError(
                "unsupported_action", QUEUE_UNAVAILABLE.format(side=side.name), side_id
            ) from exc
        except CommandError:
            raise
        except Exception as exc:  # noqa: BLE001 - vendor libraries raise their own types
            log.warning("queue read failed", extra={"extra": {"side": side_id, "error": str(exc)}})
            raise CommandError(
                "vendor_error", f"{side.name} didn't return its queue.", side_id
            ) from exc
        np = self.state.now_playing.get(side_id)
        current = None
        if np is not None and np.track_id:
            current = next((e.index for e in entries if e.track_id == np.track_id), None)
        queue = QueueState(
            items=entries,
            current_index=current,
            source=np.source if np else None,
            total=total,
            truncated=total is None or total > len(entries),
        )
        self.store.set_queue(side_id, queue)
        return queue

    async def queue_jump(self, target: str, index: int) -> Ack:
        async def run(out: _Outcomes) -> None:
            if index < 0:
                raise CommandError(
                    "invalid_argument", "Queue position must be zero or more.", target
                )
            for side in resolve_sides(self.state, target):
                await out.attempt(side.id, self._jump_side(side, index, target))

        return await self._execute("queue_jump", target, run)

    async def _jump_side(self, side: Side, index: int, target: str) -> None:
        adapter = self._adapter_for(side.vendor, target)
        self._require_online(side.coordinator_player_id, target)
        queued = self.state.queues.get(side.id)
        if queued is not None and queued.total is not None and index >= queued.total:
            raise CommandError(
                "invalid_argument", f"{side.name}'s queue has {queued.total} tracks.", target
            )
        try:
            await self._call(
                lambda: adapter.play_queue_index(side.coordinator_player_id, index),
                "queue_jump",
                target,
                side.name,
                vendor=side.vendor,
            )
        except CommandError as exc:
            if exc.code == "vendor_error" and isinstance(exc.__cause__, IndexError):
                raise CommandError(
                    "invalid_argument", f"{side.name}'s queue is shorter than that.", target
                ) from exc
            raise

    async def play_mode(
        self, target: str, *, shuffle: bool | None = None, repeat: str | None = None
    ) -> Ack:
        async def run(out: _Outcomes) -> None:
            if shuffle is None and repeat is None:
                raise CommandError(
                    "invalid_argument", "Choose shuffle, repeat, or both to change.", target
                )
            if self.sync_active():
                raise CommandError("sync_active", PLAY_MODE_LOCKED, target)
            for side in resolve_sides(self.state, target):
                current = side.play_mode
                mode = PlayMode(
                    shuffle=current.shuffle if shuffle is None else shuffle,
                    repeat=current.repeat if repeat is None else repeat,  # type: ignore[arg-type]
                )
                await out.attempt(side.id, self._play_mode_side(side, mode, target))

        return await self._execute("play_mode", target, run)

    async def _play_mode_side(self, side: Side, mode: PlayMode, target: str) -> None:
        adapter = self._adapter_for(side.vendor, target)
        self._require_online(side.coordinator_player_id, target)
        await self._call(
            lambda: adapter.set_play_mode(side.coordinator_player_id, mode),
            "play_mode",
            target,
            side.name,
            vendor=side.vendor,
        )
        for pid in side.member_ids:
            self.store.set_play_mode(pid, mode)

    def queue_entries(self, side_id: str) -> list[QueueEntry]:
        queued = self.state.queues.get(side_id)
        return list(queued.items) if queued else []

    # -- internals ------------------------------------------------------------------

    @property
    def state(self) -> HubState:
        return self.store.state

    def _adapter_for(self, vendor: str, target: str) -> PlaybackAdapter:
        adapter = self.playback.get(vendor)
        if adapter is None or adapter.status().state != "connected":
            raise CommandError(
                "adapter_disconnected",
                f"{_name(self.state, target)} didn't respond; the {vendor_label(vendor)} link "
                "is down.",
                target,
            )
        return adapter

    def _require_online(self, player_id: str, target: str) -> None:
        p = self.state.players.get(player_id)
        if p is not None and not p.online:
            raise CommandError("device_offline", f"{p.name} is offline.", target)

    def _require_seekable(self, side: Side, target: str) -> None:
        if not side.capabilities.supports_seek:
            np = self.state.now_playing.get(side.id)
            if np is not None and not np.seekable:
                raise CommandError(
                    "not_seekable", f"{side.name} is playing a station; it can't seek.", target
                )
            raise CommandError(
                "unsupported_action", f"{side.name} can't seek from this hub.", target
            )

    async def _set_player_volume(self, player: Player, level: int, target: str) -> None:
        self._require_online(player.id, target)
        pid, name, vendor = player.id, player.name, player.vendor
        zone = self._volume_zone(player, target)
        if zone is not None:
            denon = self._require_denon(zone, target)
            zid = zone.id
            await self.coalescer.submit(
                f"volume:{pid}",
                lambda: self._call(
                    lambda: denon.set_volume(zid, level), "volume", target, name, vendor=vendor
                ),
            )
            return
        adapter = self._adapter_for(vendor, target)
        await self.coalescer.submit(
            f"volume:{pid}",
            lambda: self._call(
                lambda: adapter.set_volume(pid, level), "volume", target, name, vendor=vendor
            ),
        )

    def _volume_zone(self, player: Player, target: str) -> Zone | None:
        """The amplifier zone that owns this player's level, or None for a self-mixing player.

        Routing volume and mute here is not an optimisation: HEOS accepts set_volume on an
        AVR-hosted player, reports success, and changes nothing (verified on an AVR-X3400H).
        """
        zone = zone_for_player(self.state, player.id)
        if zone is None:
            return None
        if not zone.supports_volume:
            raise CommandError(
                "unsupported_action",
                f"{zone.name} feeds an external amplifier; set the volume there.",
                target,
            )
        return zone

    def _require_denon(self, zone: Zone, target: str) -> DenonAdapter:
        if self.denon is None or self.denon.status().state != "connected":
            raise CommandError(
                "adapter_disconnected",
                f"{zone.name} didn't change; the amplifier link is down.",
                target,
            )
        if not zone.online:
            raise CommandError("device_offline", f"{zone.name} is not answering.", target)
        return self.denon

    async def _transport_on(self, adapter: PlaybackAdapter, side: Side, action: str) -> None:
        coord = side.coordinator_player_id
        if action == "toggle":
            action = "pause" if side.play_state == "play" else "play"
        if action == "play":
            await adapter.play(coord)
        elif action == "pause":
            await adapter.pause(coord)
        elif action == "stop":
            await adapter.stop(coord)
        elif action == "next":
            await adapter.next(coord)
        elif action == "prev":
            await adapter.previous(coord)
        else:  # pragma: no cover - guarded by the Literal type at the API layer
            raise CommandError("invalid_argument", f"Unknown transport action {action!r}.")

    async def _call(
        self,
        fn: Callable[[], Awaitable[Any]],
        action: str,
        target: str,
        name: str,
        vendor: str | None = None,
    ) -> None:
        """Run one adapter call, mapping protocol failures onto the error catalogue. Successful
        round-trips feed :attr:`latency` per vendor."""
        started = time.perf_counter()
        try:
            await fn()
            if vendor:
                self.latency.record(vendor, (time.perf_counter() - started) * 1000, _kind(action))
        except CommandError:
            raise
        except UnsupportedCommandError as exc:
            raise CommandError(
                "unsupported_action", f"{name} can't {action} from this hub.", target
            ) from exc
        except Exception as exc:  # noqa: BLE001 - vendor libraries raise their own types
            log.warning(
                "adapter command failed",
                extra={"extra": {"action": action, "target": target, "error": str(exc)}},
            )
            raise CommandError(
                "vendor_error", f"{_verb(action)} didn't happen on {name}.", target
            ) from exc

    async def _execute(self, action: str, target: str | None, run: Run) -> Ack:
        """Run a command and emit exactly one ack, whatever happens inside."""
        cid = correlation_id.get() or "-"
        started = time.perf_counter()
        out = _Outcomes()
        error: CommandError | None = None
        try:
            await run(out)
        except CommandError as exc:
            error = exc
        except Exception:  # noqa: BLE001 - never leak: the caller needs an ack
            log.exception("command crashed", extra={"extra": {"action": action}})
            error = CommandError("vendor_error", f"{_verb(action)} failed unexpectedly.", target)
        if error is None and out.all_failed:
            error = out.failures[0]
        ok = error is None
        partial = (
            []
            if not out.failures or (out.all_failed and len(out.failures) == 1)
            else [
                PartialFailure(target=f.target or "-", code=f.code, message=f.message)
                for f in out.failures
            ]
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        ack = Ack(
            correlation_id=cid,
            ok=ok,
            action=action,
            target=target,
            state_version=self.state.version,
            error=(
                ErrorEnvelope(
                    code=error.code,
                    message=error.message,
                    target=error.target or target,
                    correlation_id=cid,
                )
                if error
                else None
            ),
            partial=partial,
            applied=list(out.applied),
            resolved=dict(out.resolved),
            warnings=list(out.warnings),
            latency_ms=latency_ms,
        )
        log.info(
            "command",
            extra={
                "extra": {
                    "action": action,
                    "target": target,
                    "ok": ack.ok,
                    "latency_ms": latency_ms,
                    "partial": len(partial),
                    **({"code": error.code} if error else {}),
                }
            },
        )
        self._broadcast(ack)
        return ack

    def _broadcast(self, ack: Ack) -> None:
        from .tasks import spawn

        for cb in list(self._ack_callbacks):
            try:
                result = cb(ack)
            except Exception:  # noqa: BLE001
                log.exception("ack callback failed")
                continue
            if asyncio.iscoroutine(result):
                spawn(result, self._tasks)


def _kind(action: str) -> str:
    if action in ("play", "pause", "toggle", "stop", "next", "prev"):
        return "transport"
    if action in ("seek", "skip"):
        return "seek"
    if action in ("volume", "linked_volume", "mute"):
        return "volume"
    if action in ("queue_jump", "play_mode"):
        return "transport"
    return action


def _verb(action: str) -> str:
    return {
        "play": "Play",
        "pause": "Pause",
        "toggle": "Play/pause",
        "stop": "Stop",
        "next": "Next track",
        "prev": "Previous track",
        "seek": "Seek",
        "skip": "Skip",
        "volume": "Volume",
        "linked_volume": "Linked volume",
        "mute": "Mute",
        "zone_power": "Power",
        "group": "Grouping",
        "ungroup": "Ungrouping",
        "play_content": "Play",
        "play_station": "Play",
        "sync_play": "Sync Play",
        "sync_stop": "Sync Play stop",
        "sync_retry": "Sync Play retry",
        "queue_jump": "Jump in the queue",
        "play_mode": "Shuffle/repeat",
    }.get(action, action.capitalize())


def http_status(code: str) -> int:
    return ERROR_CATALOGUE.get(code, (500, ""))[0]


def catalogue() -> Iterable[tuple[str, int, str]]:
    for code, (status, meaning) in ERROR_CATALOGUE.items():
        yield code, status, meaning
