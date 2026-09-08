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
SyncStatus = Literal[
    "idle",
    "resolving",
    "priming",
    "verifying",
    "starting",
    "locked",
    "drifting",
    "correcting",
    "lost",
    "stopped",
]


def now() -> datetime:
    return datetime.now(UTC)


class ArtRef(BaseModel):
    """Artwork as served by the hub art proxy (design system §2.2, §11).

    ``url`` is hub-relative (``/api/art/{cache_key}``); append ``?size=96|320|1080|backdrop``.
    ``accent`` is the clamped art-derived colour, ``None`` until the proxy has analysed the image.
    ``accent_is_safe`` is the hub-side contrast check; when false the client uses ``bg-raised``.
    """

    url: str | None = None
    accent: str | None = None
    accent_is_safe: bool = False
    cache_key: str | None = None


Art = ArtRef  # backwards-compatible alias (Phase 0/1 name)


Service = Literal["tidal", "ytmusic", "pandora"]

# User-facing names for the two ecosystems. Every message a person can read (error envelopes,
# partial failures, warnings, needs_link) goes through this map; raw vendor ids never do.
VENDOR_LABEL: dict[str, str] = {"heos": "HEOS", "sonos": "Sonos", "denon": "Denon"}


def vendor_label(vendor: str) -> str:
    return VENDOR_LABEL.get(vendor, vendor.title())


SERVICE_LABEL: dict[str, str] = {"tidal": "Tidal", "ytmusic": "YouTube Music", "pandora": "Pandora"}


def service_label(service: str) -> str:
    """User-facing name of a streaming service (never the raw id in copy)."""
    return SERVICE_LABEL.get(service, service.title())


ContentKind = Literal["album", "playlist", "track", "station"]


class ContentRef(BaseModel):
    """Canonical service id: what the hub browses by and plays from ("browse once, play
    anywhere", PRD review §2.1). Also the cross-vendor identity of the current track."""

    service: Service
    kind: ContentKind
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]+$", max_length=200)

    @property
    def key(self) -> str:
        return f"{self.service}:{self.kind}:{self.id}"


class Capabilities(BaseModel):
    """What a player or side can do, so Phase 1 does not hard-code vendor checks.

    ``supports_next`` / ``supports_prev`` follow the current source (HEOS ``supported_controls``,
    Sonos DIDL item class): a radio station has no next track.
    """

    supports_seek: bool = True
    supports_power: bool = False
    can_group: bool = True
    supports_next: bool = True
    supports_prev: bool = True


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
    volume: int = Field(default=0, ge=0, le=100)
    muted: bool = False
    online: bool = True
    # A zone feeding an external amp on a fixed pre-out cannot be attenuated by the receiver.
    # The UI must hide the slider rather than offer a control that does nothing.
    supports_volume: bool = True
    supports_mute: bool = True
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
    volume: int = 0  # coordinator volume for solo sides; member average for groups (Sonos style)
    muted: bool = False  # every member muted
    capabilities: Capabilities = Field(default_factory=Capabilities)


class NowPlaying(BaseModel):
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    art: Art = Field(default_factory=Art)
    source: str | None = None
    seekable: bool = True
    supports_next: bool = True
    supports_prev: bool = True
    duration_ms: int | None = None
    track_id: str | None = None  # vendor-native id: HEOS media id, Sonos track URI. Never
    # compare across vendors; use ``content_ref``.
    content_ref: ContentRef | None = None  # canonical service track id when known (Tidal)


class Position(BaseModel):
    position_ms: int = 0
    reported_at: datetime = Field(default_factory=now)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


def optimistic_position(position_ms: int) -> Position:
    """A low-confidence position adapters write right after a seek or track change, so the UI
    lands where the user put it until the next device report confirms."""
    return Position(position_ms=position_ms, confidence=0.5)


class SyncState(BaseModel):
    """Sync Play session as streamed to clients (docs/api.md → Sync Play, docs/sync-engine.md).

    ``master_side`` is always the HEOS side (it cannot be seeked, so it is the clock);
    ``follower_side`` the Sonos side. ``drift_ms`` is follower minus master.
    """

    status: SyncStatus = "idle"
    session_id: str | None = None
    master_side: str | None = None
    follower_side: str | None = None
    content_ref: ContentRef | None = None
    title: str | None = None
    drift_ms: int | None = None
    start_delta_ms: int | None = None
    last_correction_at: datetime | None = None
    corrections: int = 0
    reason: str | None = None
    started_at: datetime | None = None


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


def zone_for_player(state: HubState, player_id: str) -> Zone | None:
    """The Denon zone that owns ``player_id``'s volume, if any.

    An AVR-hosted HEOS player has no usable volume of its own: HEOS returns success for
    ``set_volume`` and applies nothing, and reports 0 regardless of the receiver's real MV
    (verified on an AVR-X3400H). For those players the amplifier zone is the authority, so
    volume and mute route to the Denon adapter and its zone values mirror onto the player.
    """
    for zone in state.zones.values():
        if player_id in zone.player_ids:
            return zone
    return None


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
        member_players = [players[m] for m in members]
        return Side(
            id=sid,
            vendor=vendor,
            coordinator_player_id=coordinator.id,
            member_ids=members,
            name=name,
            play_state=coordinator.play_state,
            volume=round(sum(p.volume for p in member_players) / len(member_players)),
            muted=all(p.muted for p in member_players),
            capabilities=Capabilities(
                # The protocol must support it *and* the current source must allow it.
                supports_seek=coordinator.capabilities.supports_seek
                and (np.seekable if np else True),
                supports_power=any(
                    m in powered or players[m].capabilities.supports_power for m in members
                ),
                can_group=coordinator.capabilities.can_group,
                supports_next=coordinator.capabilities.supports_next
                and (np.supports_next if np else True),
                supports_prev=coordinator.capabilities.supports_prev
                and (np.supports_prev if np else True),
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

    def update_art_accent(self, cache_key: str, accent: str | None, safe: bool) -> list[str]:
        """Patch every now-playing entry whose art has ``cache_key`` once the proxy knows the
        accent. No-op (no version bump) when nothing references the key."""

        def apply(s: HubState) -> None:
            for side_id, np in list(s.now_playing.items()):
                if np.art.cache_key == cache_key and (
                    np.art.accent != accent or np.art.accent_is_safe != safe
                ):
                    art = np.art.model_copy(update={"accent": accent, "accent_is_safe": safe})
                    s.now_playing[side_id] = np.model_copy(update={"art": art})

        return self._mutate(apply)

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
