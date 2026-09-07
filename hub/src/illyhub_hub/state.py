"""Unified hub state: players, zones, sides, now-playing, positions, connections.

Every protocol adapter writes into a :class:`StateStore`; the API and (from Phase 1) the
WebSocket read from it. A **side** is one ecosystem's native group: a HEOS group or a Sonos
group with its coordinator. Solo players are their own side.

Invariants:
- The store is mutated only from the event-loop thread. Adapters that talk to blocking
  libraries snapshot data in a worker thread and apply it on the loop.
- Listeners are notified after a mutation commits and must not assume they run inside the
  mutating call; re-entrant mutations from a listener are queued and delivered in order.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import Vendor
from .tasks import cancel_all, spawn

PlayState = Literal["play", "pause", "stop", "unknown"]
ConnState = Literal["connected", "reconnecting", "disconnected", "disabled"]
SyncStatus = Literal["idle", "priming", "locked", "drifting", "lost"]


def now() -> datetime:
    return datetime.now(UTC)


class Art(BaseModel):
    """Artwork reference as served by the hub art proxy (Phase 2 fills accent)."""

    url: str | None = None
    accent: str | None = None
    accent_is_safe: bool = False


class Capabilities(BaseModel):
    """What a player or side can do, so Phase 1 does not hard-code vendor checks."""

    supports_seek: bool = True
    supports_power: bool = False
    can_group: bool = True


class Player(BaseModel):
    id: str
    name: str
    vendor: Vendor
    ip: str | None = None
    model: str | None = None
    online: bool = True
    volume: int = Field(default=0, ge=0, le=100)
    muted: bool = False
    play_state: PlayState = "stop"
    group_id: str | None = None
    capabilities: Capabilities = Field(default_factory=Capabilities)


class Zone(BaseModel):
    """A Denon amplifier zone, namespaced by receiver host: ``denon-{host}:{key}``."""

    id: str
    key: str  # "main" | "zone2"
    name: str
    power: bool = False
    online: bool = True
    host: str | None = None
    device_id: str | None = None
    player_ids: list[str] = Field(default_factory=list)


class GroupTopology(BaseModel):
    """Raw native group as reported by an adapter. ``member_ids`` includes the coordinator."""

    id: str
    vendor: Vendor
    coordinator_player_id: str
    member_ids: list[str]
    name: str | None = None


class Side(BaseModel):
    id: str
    vendor: Vendor
    coordinator_player_id: str
    member_ids: list[str]
    name: str
    play_state: PlayState = "stop"
    volume: int = 0
    capabilities: Capabilities = Field(default_factory=Capabilities)


class NowPlaying(BaseModel):
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    art: Art = Field(default_factory=Art)
    source: str | None = None
    seekable: bool = True
    duration_ms: int | None = None
    track_id: str | None = None


class Position(BaseModel):
    position_ms: int = 0
    reported_at: datetime = Field(default_factory=now)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class SyncState(BaseModel):
    status: SyncStatus = "idle"
    side_ids: list[str] = Field(default_factory=list)
    drift_ms: int | None = None
    last_correction_at: datetime | None = None


class ConnectionStatus(BaseModel):
    state: ConnState = "disconnected"
    last_error: str | None = None
    since: datetime = Field(default_factory=now)


class HubState(BaseModel):
    version: int = 0
    players: dict[str, Player] = Field(default_factory=dict)
    zones: dict[str, Zone] = Field(default_factory=dict)
    groups: dict[str, GroupTopology] = Field(default_factory=dict)
    sides: dict[str, Side] = Field(default_factory=dict)
    now_playing: dict[str, NowPlaying] = Field(default_factory=dict)
    positions: dict[str, Position] = Field(default_factory=dict)
    sync: SyncState = Field(default_factory=SyncState)
    connections: dict[str, ConnectionStatus] = Field(default_factory=dict)


def side_id_for_group(vendor: str, group_id: str) -> str:
    return f"{vendor}:{group_id}"


def side_id_for(player: Player) -> str:
    """Solo players are their own side; grouped players share ``vendor:group_id``."""
    return side_id_for_group(player.vendor, player.group_id or player.id)


def compute_sides(
    players: dict[str, Player],
    groups: dict[str, GroupTopology],
    now_playing: dict[str, NowPlaying] | None = None,
    zones: dict[str, Zone] | None = None,
) -> dict[str, Side]:
    now_playing = now_playing or {}
    powered = {pid for z in (zones or {}).values() for pid in z.player_ids}
    sides: dict[str, Side] = {}

    def build(sid: str, vendor: Vendor, coordinator: Player, members: list[str], name: str) -> Side:
        np = now_playing.get(sid)
        return Side(
            id=sid,
            vendor=vendor,
            coordinator_player_id=coordinator.id,
            member_ids=members,
            name=name,
            play_state=coordinator.play_state,
            volume=coordinator.volume,
            capabilities=Capabilities(
                supports_seek=(np.seekable if np else coordinator.capabilities.supports_seek),
                supports_power=any(
                    m in powered or players[m].capabilities.supports_power for m in members
                ),
                can_group=coordinator.capabilities.can_group,
            ),
        )

    for group in groups.values():
        coordinator = players.get(group.coordinator_player_id)
        members = [m for m in group.member_ids if m in players]
        if coordinator is None or not members:
            continue
        sid = side_id_for_group(group.vendor, group.id)
        name = group.name or " + ".join(players[m].name for m in members)
        sides[sid] = build(sid, group.vendor, coordinator, members, name)
    grouped = {m for s in sides.values() for m in s.member_ids}
    for player in players.values():
        if player.id in grouped:
            continue
        sid = side_id_for_group(player.vendor, player.id)
        sides[sid] = build(sid, player.vendor, player, [player.id], player.name)
    return sides


def diff(old: HubState | dict[str, Any], new: HubState | dict[str, Any]) -> list[str]:
    """Changed paths at depth two, e.g. ``players.heos-1`` or ``sync``. JSON-patch-lite.

    Accepts models or their ``model_dump(mode="json")`` output so callers can reuse a cached dump.
    """
    old_d = old if isinstance(old, dict) else old.model_dump(mode="json")
    new_d = new if isinstance(new, dict) else new.model_dump(mode="json")
    changed: list[str] = []
    for key in sorted(set(old_d) | set(new_d)):
        if key == "version":
            continue
        a, b = old_d.get(key), new_d.get(key)
        if a == b:
            continue
        if isinstance(a, dict) and isinstance(b, dict) and key != "sync":
            changed.extend(
                f"{key}.{sub}" for sub in sorted(set(a) | set(b)) if a.get(sub) != b.get(sub)
            )
        else:
            changed.append(key)
    return changed


Listener = Callable[[HubState, list[str]], Any]


class StateStore:
    """Owns the single :class:`HubState` and a cached JSON dump of it.

    Mutations bump ``version`` and notify listeners *after* the commit. Listener callbacks may
    be sync or async; async ones are scheduled on the running loop and tracked so
    :meth:`aclose` can cancel them. See module docstring for threading and re-entrancy rules.
    """

    def __init__(self) -> None:
        self._state = HubState()
        self._dump: dict[str, Any] = self._state.model_dump(mode="json")
        self._listeners: list[Listener] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pending: deque[tuple[HubState, list[str]]] = deque()
        self._notifying = False

    @property
    def state(self) -> HubState:
        return self._state

    def snapshot(self) -> dict[str, Any]:
        return self._dump

    def subscribe(self, listener: Listener) -> tuple[dict[str, Any], Callable[[], None]]:
        """Register ``listener`` and return the snapshot it should start from, atomically."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return self._dump, unsubscribe

    async def aclose(self) -> None:
        await cancel_all(self._tasks)

    # -- commit / notify ------------------------------------------------------------------

    def _commit(self, new: HubState) -> list[str]:
        new_dump = new.model_dump(mode="json")
        changed = diff(self._dump, new_dump)
        if not changed:
            return []
        new.version = self._state.version + 1
        new_dump["version"] = new.version
        self._state, self._dump = new, new_dump
        self._pending.append((new, changed))
        self._flush()
        return changed

    def _flush(self) -> None:
        if self._notifying:
            return  # re-entrant mutation from a listener; the outer flush will deliver it
        self._notifying = True
        try:
            while self._pending:
                state, changed = self._pending.popleft()
                for listener in list(self._listeners):
                    self._call(listener, state, changed)
        finally:
            self._notifying = False

    def _call(self, listener: Listener, state: HubState, changed: list[str]) -> None:
        try:
            result = listener(state, changed)
        except Exception:  # noqa: BLE001 - one bad listener must not break the store
            from .logsetup import get_logger

            get_logger("state").exception("state listener failed")
            return
        if asyncio.iscoroutine(result):
            spawn(result, self._tasks)

    def _mutate(self, fn: Callable[[HubState], None]) -> list[str]:
        new = self._state.model_copy(deep=True)
        fn(new)
        new.sides = compute_sides(new.players, new.groups, new.now_playing, new.zones)
        new.now_playing = {k: v for k, v in new.now_playing.items() if k in new.sides}
        new.positions = {k: v for k, v in new.positions.items() if k in new.sides}
        return self._commit(new)

    # -- mutation helpers -------------------------------------------------------------

    def upsert_player(self, player: Player) -> list[str]:
        return self._mutate(lambda s: s.players.__setitem__(player.id, player))

    def update_player(self, player_id: str, **fields: Any) -> list[str]:
        def apply(s: HubState) -> None:
            if player_id in s.players:
                s.players[player_id] = s.players[player_id].model_copy(update=fields)

        return self._mutate(apply)

    def remove_player(self, player_id: str) -> list[str]:
        def apply(s: HubState) -> None:
            s.players.pop(player_id, None)
            for g in list(s.groups.values()):
                if player_id in g.member_ids:
                    g.member_ids = [m for m in g.member_ids if m != player_id]
                    if len(g.member_ids) < 2 or g.coordinator_player_id == player_id:
                        s.groups.pop(g.id, None)
                        for m in g.member_ids:
                            if m in s.players:
                                s.players[m] = s.players[m].model_copy(update={"group_id": None})

        return self._mutate(apply)

    def set_zone(self, zone: Zone) -> list[str]:
        return self._mutate(lambda s: s.zones.__setitem__(zone.id, zone))

    def remove_zone(self, zone_id: str) -> list[str]:
        return self._mutate(lambda s: s.zones.pop(zone_id, None))

    def set_groups(self, vendor: Vendor, groups: list[GroupTopology]) -> list[str]:
        def apply(s: HubState) -> None:
            s.groups = {k: g for k, g in s.groups.items() if g.vendor != vendor}
            for g in groups:
                s.groups[g.id] = g
                for m in g.member_ids:
                    if m in s.players:
                        s.players[m] = s.players[m].model_copy(update={"group_id": g.id})
            grouped = {m for g in groups for m in g.member_ids}
            for pid, p in s.players.items():
                if p.vendor == vendor and pid not in grouped and p.group_id is not None:
                    s.players[pid] = p.model_copy(update={"group_id": None})

        return self._mutate(apply)

    def set_now_playing(self, side_id: str, np: NowPlaying) -> list[str]:
        return self._mutate(lambda s: s.now_playing.__setitem__(side_id, np))

    def set_position(self, side_id: str, pos: Position) -> list[str]:
        return self._mutate(lambda s: s.positions.__setitem__(side_id, pos))

    def set_sync(self, sync: SyncState) -> list[str]:
        return self._mutate(lambda s: setattr(s, "sync", sync))

    def set_connection(self, adapter: str, state: ConnState, error: str | None = None) -> list[str]:
        def apply(s: HubState) -> None:
            prev = s.connections.get(adapter)
            since = prev.since if prev and prev.state == state else now()
            s.connections[adapter] = ConnectionStatus(state=state, last_error=error, since=since)

        return self._mutate(apply)

    def mark_vendor_offline(self, vendor: Vendor) -> list[str]:
        def apply(s: HubState) -> None:
            for pid, p in s.players.items():
                if p.vendor == vendor:
                    s.players[pid] = p.model_copy(update={"online": False})

        return self._mutate(apply)
