"""HEOS adapter over ``pyheos``: one persistent CLI socket, heartbeat, reconnect with backoff.

Commands map onto ``pyheos.HeosPlayer`` methods and ``Heos.set_group``. The HEOS CLI protocol
has **no seek command** (player commands stop at play/pause/stop, next/previous, volume, mute,
play modes and queue operations), so :meth:`PyHeosAdapter.seek` raises
:class:`UnsupportedCommandError` and HEOS players advertise ``supports_seek=False``. Position
comes from ``player_now_playing_progress`` events, which fire right after the device's counter
ticks, and is fed to the :class:`~illyhub_hub.positions.PositionTracker` as edge observations.

Verified only against a mocked ``pyheos.Heos``; no physical HEOS device has been exercised.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from ..config import Settings
from ..positions import PositionTracker
from ..state import (
    Art,
    Capabilities,
    GroupTopology,
    NowPlaying,
    Player,
    StateStore,
    side_id_for,
)
from ..tasks import stop_task
from .base import Backoff, HeosAdapter, UnsupportedCommandError

EVENT_PLAYERS_CHANGED = "event/players_changed"
EVENT_GROUPS_CHANGED = "event/groups_changed"
EVENT_PLAYER_STATE_CHANGED = "event/player_state_changed"
EVENT_PLAYER_VOLUME_CHANGED = "event/player_volume_changed"
EVENT_NOW_PLAYING_CHANGED = "event/player_now_playing_changed"
EVENT_NOW_PLAYING_PROGRESS = "event/player_now_playing_progress"


class HeosClient(Protocol):
    """The subset of ``pyheos.Heos`` the adapter relies on. Lets tests inject a fake."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def get_players(self, *, refresh: bool = False) -> dict[int, Any]: ...
    async def get_groups(self, *, refresh: bool = False) -> dict[int, Any]: ...
    def add_on_connected(self, cb: Callable[[], Any]) -> Callable[[], None]: ...
    def add_on_disconnected(self, cb: Callable[[], Any]) -> Callable[[], None]: ...
    def add_on_controller_event(self, cb: Callable[[str, Any], Any]) -> Callable[[], None]: ...
    async def set_group(self, player_ids: Sequence[int]) -> None: ...


HeosFactory = Callable[[str, Settings], HeosClient]


def default_heos_factory(host: str, settings: Settings) -> HeosClient:  # pragma: no cover
    from pyheos import Heos, HeosOptions

    return Heos(
        HeosOptions(
            host,
            heart_beat=True,
            heart_beat_interval=settings.heos_heartbeat_s,
            auto_reconnect=False,  # the adapter owns reconnect so backoff is observable
        )
    )


def player_id(pid: int | str) -> str:
    return f"heos-{pid}"


def raw_pid(player_id_: str) -> int:
    """``heos-12`` -> 12. Raises ValueError for ids that are not HEOS players."""
    prefix = "heos-"
    if not player_id_.startswith(prefix) or player_id_.startswith("heos-g"):
        raise ValueError(f"not a HEOS player id: {player_id_!r}")
    return int(player_id_[len(prefix) :])


def group_id(gid: int | str) -> str:
    return f"heos-g{gid}"


def _enum_value(raw: Any) -> str:
    """pyheos uses StrEnums; unwrap to the plain string (or pass strings through)."""
    value = getattr(raw, "value", raw)
    return str(value) if value is not None else ""


def _play_state(raw: Any) -> str:
    value = _enum_value(raw)
    return value if value in ("play", "pause", "stop") else "unknown"


def _controls(media: Any) -> set[str]:
    """``supported_controls`` from pyheos ``HeosNowPlayingMedia`` as plain strings; empty when
    the attribute is missing (older pyheos), in which case callers assume everything works."""
    raw = getattr(media, "supported_controls", None)
    return {_enum_value(c) for c in raw} if raw else set()


class PyHeosAdapter(HeosAdapter):
    def __init__(
        self,
        store: StateStore,
        settings: Settings,
        host: str,
        factory: HeosFactory = default_heos_factory,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(store)
        self.settings = settings
        self.host = host
        self._factory = factory
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._heos: HeosClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._backoff = Backoff(cap=settings.reconnect_max_s)
        self._player_unsubs: dict[str, Callable[[], None]] = {}
        self._raw_players: dict[str, Any] = {}
        self._trackers: dict[str, PositionTracker] = {}
        self._disconnected = asyncio.Event()
        self._closing = False

    # -- lifecycle ------------------------------------------------------------------

    async def connect(self) -> None:
        if self._task is not None:  # single-socket invariant: second connect is a no-op
            self.log.debug("connect() ignored; already connected or connecting")
            return
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="heos-connection")

    async def disconnect(self) -> None:
        self._closing = True
        await stop_task(self._task)
        self._task = None
        await self._cancel_tasks()
        await self._teardown()
        self._set_status("disconnected")

    async def _teardown(self) -> None:
        for unsub in self._player_unsubs.values():
            unsub()
        self._player_unsubs.clear()
        self._raw_players.clear()
        heos, self._heos = self._heos, None
        if heos is not None:
            try:
                await heos.disconnect()
            except Exception:  # noqa: BLE001
                self.log.debug("disconnect raised", exc_info=True)

    async def _run(self) -> None:
        while not self._closing:
            try:
                await self._connect_once()
                await self._disconnected.wait()
                if self._closing:
                    return
                if self._held_at_least(self.settings.backoff_reset_after_s):
                    self._backoff.reset()
                self.store.mark_vendor_offline("heos")
                self._set_status("reconnecting", "connection lost")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._set_status("reconnecting", str(exc))
                self.log.warning("heos connect failed", extra={"extra": {"error": str(exc)}})
            await self._teardown()
            await asyncio.sleep(self._backoff.next())

    async def _connect_once(self) -> None:
        self._disconnected = asyncio.Event()
        heos = self._factory(self.host, self.settings)
        self._heos = heos
        heos.add_on_disconnected(self._disconnected.set)
        heos.add_on_controller_event(self._on_controller_event)
        await heos.connect()
        await self._load_topology(heos)
        self._mark_connected()
        self._set_status("connected")
        self._emit("connected", host=self.host)

    # -- topology -------------------------------------------------------------------

    async def _load_topology(self, heos: HeosClient) -> None:
        """Players, then groups, then now-playing: sides must exist before media is attached."""
        players = await heos.get_players(refresh=True)
        seen: set[str] = set()
        for pid, raw in players.items():
            p = self._to_player(pid, raw)
            seen.add(p.id)
            self._raw_players[p.id] = raw
            self.store.upsert_player(p)
            if p.id not in self._player_unsubs:
                if callable(getattr(raw, "add_on_player_event", None)):
                    self._player_unsubs[p.id] = raw.add_on_player_event(
                        lambda event, _raw=raw, _pid=pid: self._on_player_event(_pid, _raw, event)
                    )
                else:
                    self.log.warning(
                        "pyheos player has no add_on_player_event; live updates disabled",
                        extra={"extra": {"player": p.id}},
                    )
        for stale in [pid for pid in self.store.state.players if pid.startswith("heos-")]:
            if stale not in seen:
                self.store.remove_player(stale)
                self._raw_players.pop(stale, None)
                unsub = self._player_unsubs.pop(stale, None)
                if unsub:
                    unsub()
        await self._load_groups(heos)
        for pid, raw in players.items():
            p = self.store.state.players.get(player_id(pid))
            if p is not None:
                self._apply_now_playing(p, raw)

    async def _load_groups(self, heos: HeosClient) -> None:
        groups = await heos.get_groups(refresh=True)
        topo: list[GroupTopology] = []
        for gid, g in groups.items():
            lead = player_id(g.lead_player_id)
            # pyheos keeps the leader out of member_player_ids; the hub's topology includes it.
            followers = [player_id(m) for m in g.member_player_ids if player_id(m) != lead]
            topo.append(
                GroupTopology(
                    id=group_id(gid),
                    vendor="heos",
                    coordinator_player_id=lead,
                    member_ids=[lead, *followers],
                    name=getattr(g, "name", None),
                )
            )
        self.store.set_groups("heos", topo)

    def _to_player(self, pid: int, raw: Any) -> Player:
        existing = self.store.state.players.get(player_id(pid))
        return Player(
            id=player_id(pid),
            name=raw.name,
            vendor="heos",
            ip=getattr(raw, "ip_address", None),
            model=getattr(raw, "model", None),
            online=bool(getattr(raw, "available", True)),
            volume=int(getattr(raw, "volume", 0) or 0),
            muted=bool(getattr(raw, "is_muted", False)),
            play_state=_play_state(getattr(raw, "state", "unknown")),  # type: ignore[arg-type]
            group_id=existing.group_id if existing else None,
            # The HEOS CLI cannot seek; see module docstring.
            capabilities=Capabilities(supports_power=True, supports_seek=False),
        )

    # -- commands (Phase 1) ---------------------------------------------------------

    def _raw(self, player_id_: str) -> Any:
        raw = self._raw_players.get(player_id_)
        if raw is None or self._heos is None:
            raise ConnectionError(f"HEOS player {player_id_!r} is not available")
        return raw

    async def play(self, player_id: str) -> None:
        await self._raw(player_id).play()

    async def pause(self, player_id: str) -> None:
        await self._raw(player_id).pause()

    async def stop(self, player_id: str) -> None:
        await self._raw(player_id).stop()

    async def next(self, player_id: str) -> None:
        await self._raw(player_id).play_next()

    async def previous(self, player_id: str) -> None:
        await self._raw(player_id).play_previous()

    async def seek(self, player_id: str, position_ms: int) -> None:
        raise UnsupportedCommandError("the HEOS CLI protocol has no seek command")

    async def set_volume(self, player_id: str, level: int) -> None:
        await self._raw(player_id).set_volume(level)

    async def set_mute(self, player_id: str, muted: bool) -> None:
        await self._raw(player_id).set_mute(muted)

    async def set_group(self, member_ids: list[str]) -> None:
        """``group/set_group`` with the desired membership, leader first.

        HEOS applies the list as the new group in one call (players not listed leave, new ones
        join), so no diffing is needed here. A single id removes that player from its group;
        if it is the leader, HEOS dissolves the group.
        """
        heos = self._heos
        if heos is None:
            raise ConnectionError("HEOS is not connected")
        await heos.set_group([raw_pid(m) for m in member_ids])

    async def dissolve(self, coordinator_id: str, member_ids: list[str]) -> None:
        """HEOS dissolves a group only via the *leader's* pid (pyheos ``remove_group``)."""
        heos = self._heos
        if heos is None:
            raise ConnectionError("HEOS is not connected")
        await heos.set_group([raw_pid(coordinator_id)])

    def _side_id(self, pid: int) -> str | None:
        p = self.store.state.players.get(player_id(pid))
        return side_id_for(p) if p else None

    def _apply_now_playing(self, p: Player, raw: Any) -> None:
        media = getattr(raw, "now_playing_media", None)
        if media is None:
            return
        duration = getattr(media, "duration", None)
        controls = _controls(media)
        is_station = _enum_value(getattr(media, "type", "")) == "station"
        self.store.set_now_playing(
            side_id_for(p),
            NowPlaying(
                art=Art(url=getattr(media, "image_url", None)),
                title=getattr(media, "song", None),
                artist=getattr(media, "artist", None),
                album=getattr(media, "album", None),
                source=str(getattr(media, "source_id", "") or "") or None,
                seekable=not is_station,  # the CLI cannot seek anyway; kept for the UI badge
                supports_next="play_next" in controls if controls else not is_station,
                supports_prev="play_previous" in controls if controls else not is_station,
                duration_ms=int(duration) if duration else None,
                track_id=str(getattr(media, "media_id", "") or "") or None,
            ),
        )

    # -- events ---------------------------------------------------------------------

    def _on_controller_event(self, event: str, _data: Any = None) -> None:
        if event in (EVENT_PLAYERS_CHANGED, EVENT_GROUPS_CHANGED):
            self._spawn(self._refresh_topology())

    async def _refresh_topology(self) -> None:
        heos = self._heos
        if heos is None:
            return
        try:
            await self._load_topology(heos)
            self._emit("topology")
        except Exception as exc:  # noqa: BLE001
            self.log.warning("topology refresh failed", extra={"extra": {"error": str(exc)}})

    def _on_player_event(self, pid: int, raw: Any, event: str) -> None:
        try:
            self._handle_player_event(pid, raw, event)
        except Exception:  # noqa: BLE001 - a bad event must not kill the pyheos dispatcher
            self.log.exception("player event failed", extra={"extra": {"event": event}})

    def _handle_player_event(self, pid: int, raw: Any, event: str) -> None:
        p_id = player_id(pid)
        if event == EVENT_PLAYER_STATE_CHANGED:
            self.store.update_player(p_id, play_state=_play_state(getattr(raw, "state", "unknown")))
            self._emit("play_state", player_id=p_id)
        elif event == EVENT_PLAYER_VOLUME_CHANGED:
            self.store.update_player(
                p_id, volume=int(raw.volume), muted=bool(getattr(raw, "is_muted", False))
            )
            self._emit("volume", player_id=p_id)
        elif event == EVENT_NOW_PLAYING_CHANGED:
            p = self.store.state.players.get(p_id)
            if p:
                self._apply_now_playing(p, raw)
                self._emit("now_playing", player_id=p_id)
        elif event == EVENT_NOW_PLAYING_PROGRESS:
            media = getattr(raw, "now_playing_media", None)
            pos = getattr(media, "current_position", None)
            side = self._side_id(pid)
            if pos is not None and side:
                self._observe_position(side, int(pos), p_id)

    def _observe_position(self, side: str, position_ms: int, p_id: str) -> None:
        """Progress events arrive right after the device ticks: an edge observation."""
        tracker = self._trackers.setdefault(side, PositionTracker())
        loop_now = self._clock()
        playing = self.store.state.players[p_id].play_state == "play"
        tracker.set_playing(playing, loop_now)
        tracker.observe(position_ms, loop_now, is_edge=True)
        position = tracker.to_position(loop_now)
        if position is not None:
            self.store.set_position(side, position)
