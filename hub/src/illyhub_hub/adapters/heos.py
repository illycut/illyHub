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
import contextlib
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from ..art import ArtHelper, ArtHints
from ..config import Settings
from ..content import ContentRef
from ..positions import PositionTracker
from ..state import (
    Capabilities,
    GroupTopology,
    NowPlaying,
    Player,
    PlayMode,
    QueueEntry,
    StateStore,
    optimistic_position,
    queue_state_for,
    side_id_for,
    zone_for_player,
)
from ..tasks import stop_task
from .base import (
    Backoff,
    ContentUnavailableError,
    HeosAdapter,
    PlayableTrack,
    PrimedQueue,
    UnsupportedCommandError,
    VendorStation,
)

EVENT_PLAYERS_CHANGED = "event/players_changed"
EVENT_GROUPS_CHANGED = "event/groups_changed"
EVENT_PLAYER_STATE_CHANGED = "event/player_state_changed"
EVENT_PLAYER_VOLUME_CHANGED = "event/player_volume_changed"
EVENT_NOW_PLAYING_CHANGED = "event/player_now_playing_changed"
EVENT_NOW_PLAYING_PROGRESS = "event/player_now_playing_progress"
EVENT_QUEUE_CHANGED = "event/player_queue_changed"
EVENT_REPEAT_MODE_CHANGED = "event/repeat_mode_changed"
EVENT_SHUFFLE_MODE_CHANGED = "event/shuffle_mode_changed"
QUEUE_DEBOUNCE_S = 0.5  # coalesce bursts of queue events into one re-read


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
    async def get_music_sources(self, *, refresh: bool = False) -> dict[int, Any]: ...
    async def browse(
        self,
        source_id: int,
        container_id: str | None = None,
        range_start: int | None = None,
        range_end: int | None = None,
    ) -> Any: ...
    async def play_station(
        self, player_id: int, source_id: int, container_id: str | None, media_id: str
    ) -> None: ...


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


# HEOS wraps a service's own ids in its browse hierarchy rather than accepting them bare.
# Verified on a Denon AVR-X3400H against its live Tidal library (sid 10):
#
#   album     "Awaken, My Love!"    -> LIBALBUM-390695104
#   playlist  "1995 Hip-Hop Hits"   -> LIBPLAYLIST-045e426d-4ce9-44ae-8cff-6fbf20916e50
#   artist    "Beatrice Verdi"      -> LIBARTIST-7018022
#
# The suffix is exactly the Tidal id the hub already holds, so no browse is needed at play time.
# Sending the bare id (what this adapter did) is refused with HEOS error 14, "Media can't be
# played" -- which is what "Tidal won't play on the amp" was. docs/spikes/tidal-refs.md guessed
# the bare id and said so; this replaces the guess.
HEOS_CONTAINER_PREFIX = {"album": "LIBALBUM-", "playlist": "LIBPLAYLIST-", "artist": "LIBARTIST-"}


def heos_container_id(kind: str, content_id: str) -> str:
    """HEOS ``cid`` for a service container. Unknown kinds pass through unchanged."""
    return f"{HEOS_CONTAINER_PREFIX.get(kind, '')}{content_id}"


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


def _play_state(raw: Any) -> str | None:
    """Map a HEOS play state, or None when it is not one of play/pause/stop.

    HEOS answers ``state=unknown`` for about a second while a stream starts (measured on an
    AVR-X3400H: ``t+01s unknown`` then ``t+02s play``). Callers hold the state they already
    have rather than writing it through; see the Sonos adapter for the same reasoning.
    """
    value = _enum_value(raw)
    return value if value in ("play", "pause", "stop") else None


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
        art: ArtHelper | None = None,
    ) -> None:
        super().__init__(store, art=art)
        self.settings = settings
        self.host = host
        self._factory = factory
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._heos: HeosClient | None = None
        self._sources: dict[int, bool] = {}
        self._task: asyncio.Task[None] | None = None
        self._backoff = Backoff(cap=settings.reconnect_max_s)
        self._player_unsubs: dict[str, Callable[[], None]] = {}
        self._raw_players: dict[str, Any] = {}
        self._trackers: dict[str, PositionTracker] = {}
        self._primed_qid: dict[str, int] = {}  # player id -> queue id captured at prime time
        # side id -> (station ref, station name): the ref rides on now_playing.content_ref while
        # the receiver still reports that station (Phase 5).
        self._station_playing: dict[str, tuple[ContentRef, str]] = {}
        self._queue_refresh: dict[str, asyncio.Task[None]] = {}  # side id -> debounced re-read
        self._queue_ids: dict[str, list[int]] = {}  # player id -> receiver queue ids, last read
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
        self._sources = {}
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

    HEOS_SERVICE_SIDS = {"pandora": 1, "spotify": 4, "tidal": 10, "amazon": 13}

    async def _load_sources(self, heos: HeosClient) -> None:
        """``browse/get_music_sources``: which streaming services the HEOS account has linked.
        Missing on older fakes/clients → no services (never a crash)."""
        getter = getattr(heos, "get_music_sources", None)
        if getter is None:
            return
        try:
            sources = await getter(refresh=True)
        except Exception as exc:  # noqa: BLE001 - optional feature
            self.log.warning("music sources unavailable", extra={"extra": {"error": str(exc)}})
            return
        self._sources = {
            int(sid): bool(getattr(src, "available", True)) for sid, src in dict(sources).items()
        }

    def service_linked(self, service: str) -> bool:
        sid = self.HEOS_SERVICE_SIDS.get(service)
        return bool(sid is not None and self._sources.get(sid, False))

    async def play_content(
        self, player_id: str, ref: ContentRef, tracks: list[PlayableTrack], start_index: int = 0
    ) -> None:
        """Replace-and-play via ``browse/add_to_queue`` (HEOS CLI 4.4.11/4.4.12).

        Tidal on HEOS is source id 10. Container ids are the Tidal id behind a HEOS prefix
        (``LIBALBUM-``/``LIBPLAYLIST-``, see :func:`heos_container_id`); media ids for tracks are
        the bare Tidal id. Verified on an AVR-X3400H.
        Starting mid-list uses ``player/play_queue`` after the container loads.
        """
        if not self.service_linked(ref.service):
            raise ContentUnavailableError(f"{ref.service} is not linked in the HEOS app")
        if not 0 <= start_index < len(tracks):
            raise IndexError(f"start_index {start_index} out of range for {len(tracks)} tracks")
        raw = self._raw(player_id)
        self._station_playing.pop(self._side_id(raw_pid(player_id)) or "", None)
        sid = self.HEOS_SERVICE_SIDS[ref.service]
        from pyheos.types import AddCriteriaType

        replace = AddCriteriaType.REPLACE_AND_PLAY
        if ref.kind == "track":
            track = tracks[start_index]
            cid = heos_container_id("album", track.album_id) if track.album_id else ""
            await raw.add_to_queue(sid, cid, media_id=ref.id, add_criteria=replace)
            return
        await raw.add_to_queue(sid, heos_container_id(ref.kind, ref.id), add_criteria=replace)
        if start_index > 0:
            play_queue = getattr(raw, "play_queue", None)
            if play_queue is None:
                raise RuntimeError("HEOS client cannot play a queue position; start_index dropped")
            items = await self._wait_for_queue(raw, start_index + 1)
            await play_queue(_queue_id(items[start_index], start_index))

    # -- radio stations (Phase 5, PRD PAN-1/PAN-2; docs/spikes/pandora-refs.md) -------------

    MAX_STATION_CONTAINERS = 4  # e.g. Pandora's "My Stations" folder; one level deep only

    async def list_stations(self, service: str) -> list[VendorStation]:
        """``browse/browse`` on the service's source (Pandora = sid 1). Stations come back as
        ``station`` media items, either at the root or one container down. **Unverified on
        hardware**: the HEOS CLI spec lists this call but the Pandora tree shape is assumed."""
        if not self.service_linked(service):
            raise ContentUnavailableError(f"{service} is not linked in the HEOS app")
        heos = self._heos
        browse = getattr(heos, "browse", None)
        if heos is None or browse is None:
            raise ContentUnavailableError("the HEOS connection is not ready")
        sid = self.HEOS_SERVICE_SIDS[service]
        root = await browse(sid)
        stations: list[VendorStation] = []
        seen: set[tuple[str, str]] = set()
        containers: list[Any] = []

        def add(item: Any) -> None:
            st = _station_from(item, sid, service)
            key = (st.ids["cid"], st.ids["mid"])
            if key not in seen:
                seen.add(key)
                stations.append(st)

        for item in getattr(root, "items", []) or []:
            if _is_station(item):
                add(item)
            elif getattr(item, "browsable", False) and getattr(item, "container_id", None):
                containers.append(item)
        for container in containers[: self.MAX_STATION_CONTAINERS]:
            try:
                sub = await browse(sid, container.container_id)
            except Exception as exc:  # noqa: BLE001 - one folder failing must not hide the rest
                self.log.warning(
                    "station container browse failed",
                    extra={
                        "extra": {
                            "container": getattr(container, "name", container.container_id),
                            "error": str(exc),
                        }
                    },
                )
                continue
            for i in getattr(sub, "items", []) or []:
                if _is_station(i):
                    add(i)
        return stations

    async def play_station(self, player_id: str, ref: ContentRef, station: VendorStation) -> None:
        """``browse/play_stream`` (HEOS CLI 4.4.7) with the station's own sid/cid/mid. Pandora
        picks the tracks; the hub only chooses the station (PRD §3.5)."""
        if not self.service_linked(ref.service):
            raise ContentUnavailableError(f"{ref.service} is not linked in the HEOS app")
        self._raw(player_id)  # unknown player -> ConnectionError, like the other commands
        play = getattr(self._heos, "play_station", None)
        if play is None:
            raise ContentUnavailableError("the HEOS connection cannot play stations")
        mid = station.ids.get("mid")
        if not mid:
            raise ContentUnavailableError(f"{station.name} has no HEOS media id")
        side = self._side_id(raw_pid(player_id))
        if side is None:  # pragma: no cover - the player was resolved by _raw above
            return
        previous = self.store.state.now_playing.get(side)
        previous_station = self._station_playing.get(side)
        # Optimistic first (like the other commands); the receiver's event confirms with the
        # first track, or the failure below puts the previous state back.
        self._station_playing[side] = (ref, station.name)
        self.store.set_now_playing(
            side,
            NowPlaying(
                title=station.name,
                art=self.art.ref(ArtHints(device_url=station.art_url, service=ref.service)),
                source=ref.service,
                seekable=False,
                supports_next=True,
                supports_prev=False,
                duration_ms=None,
                content_ref=ref,
            ),
        )
        self.store.set_position(side, optimistic_position(0))
        for member in self.store.state.sides[side].member_ids:
            self.store.update_player(member, play_state="play")
        try:
            await play(
                raw_pid(player_id),
                int(station.ids.get("sid") or self.HEOS_SERVICE_SIDS[ref.service]),
                station.ids.get("cid") or None,
                mid,
            )
        except Exception:
            if previous_station is None:
                self._station_playing.pop(side, None)
            else:
                self._station_playing[side] = previous_station
            if previous is not None:
                self.store.set_now_playing(side, previous)
            raise

    async def prime_content(
        self, player_id: str, ref: ContentRef, tracks: list[PlayableTrack], start_index: int = 0
    ) -> PrimedQueue:
        """Sync Play priming: ``player/clear_queue`` then ``browse/add_to_queue`` with *add to
        end* (aid 3), which loads the container **without starting playback**, then wait until
        ``get_queue`` shows the start position and describe what sits there. **Unverified on
        hardware** (docs/sync-engine.md)."""
        if not self.service_linked(ref.service):
            raise ContentUnavailableError(f"{ref.service} is not linked in the HEOS app")
        if not 0 <= start_index < len(tracks):
            raise IndexError(f"start_index {start_index} out of range for {len(tracks)} tracks")
        raw = self._raw(player_id)
        sid = self.HEOS_SERVICE_SIDS[ref.service]
        from pyheos.types import AddCriteriaType

        add_to_end = AddCriteriaType.ADD_TO_END
        clear_queue = getattr(raw, "clear_queue", None)
        if clear_queue is None:
            raise RuntimeError("HEOS client cannot clear the queue; priming unavailable")
        await clear_queue()
        if ref.kind == "track":
            track = tracks[start_index]
            cid = heos_container_id("album", track.album_id) if track.album_id else ""
            await raw.add_to_queue(sid, cid, media_id=ref.id, add_criteria=add_to_end)
            needed = 1
        else:
            cid = heos_container_id(ref.kind, ref.id)
            await raw.add_to_queue(sid, cid, add_criteria=add_to_end)
            needed = start_index + 1
        items = await self._wait_for_queue(raw, needed)
        item = items[needed - 1]
        # ADD_TO_END must not start playback; some firmware resumes anyway, so park it.
        pause = getattr(raw, "pause", None)
        if pause is not None:
            with contextlib.suppress(Exception):
                await pause()
        expected = tracks[start_index]
        self._primed_qid[player_id] = _queue_id(item, needed - 1)
        return PrimedQueue(
            track_id=str(getattr(item, "media_id", None) or expected.track_id),
            title=str(getattr(item, "song", None) or expected.title),
        )

    async def start_primed(self, player_id: str, start_index: int = 0) -> None:
        """Sync Play fire: ``player/play_queue`` with the queue id captured at prime time (1-based
        on the receiver) starts the primed queue at track 0 ms."""
        raw = self._raw(player_id)
        play_queue = getattr(raw, "play_queue", None)
        if play_queue is None:
            raise RuntimeError("HEOS client cannot play a queue position")
        await play_queue(self._primed_qid.get(player_id, start_index + 1))

    QUEUE_WAIT_S = 5.0

    async def _wait_for_queue(self, raw: Any, needed: int) -> list[Any]:
        """A container is added asynchronously; poll ``player/get_queue`` (a **0-based** range,
        pyheos ``get_queue(range_start, range_end)``) until at least ``needed`` items are present
        (or time out). Returns the items ``[0, needed)``."""
        get_queue = getattr(raw, "get_queue", None)
        if get_queue is None:
            raise RuntimeError("HEOS client cannot read the queue; start_index dropped")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.QUEUE_WAIT_S
        while True:
            items = list(await get_queue(0, needed - 1))
            if len(items) >= needed:
                return items[:needed]
            if loop.time() >= deadline:
                raise TimeoutError(f"HEOS queue has {len(items)} items, needed {needed}")
            await asyncio.sleep(0.2)

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
        await self._load_sources(heos)
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
            # An AVR-hosted player's volume belongs to the Denon zone (HEOS reports 0 for it),
            # so keep whatever the Denon adapter mirrored rather than overwriting it.
            volume=self._reported_volume(player_id(pid), int(getattr(raw, "volume", 0) or 0)),
            muted=self._reported_mute(player_id(pid), bool(getattr(raw, "is_muted", False))),
            play_state=(  # keep a known state across a transient report
                _play_state(getattr(raw, "state", None))
                or (existing.play_state if existing else "stop")
            ),  # type: ignore[arg-type]
            group_id=existing.group_id if existing else None,
            # The HEOS CLI cannot seek; see module docstring.
            capabilities=Capabilities(supports_power=True, supports_seek=False),
        )

    def _zone_owns_volume(self, p_id: str) -> bool:
        """True when a Denon zone is the authority for this player's level.

        HEOS on an AVR answers ``set_volume`` with success and applies nothing, and reports 0
        for ``get_volume`` no matter what the receiver's MV is (verified on an AVR-X3400H). If
        the hub trusted those numbers it would show a dead slider and skew linked volume.
        """
        zone = zone_for_player(self.store.state, p_id)
        return zone is not None and zone.supports_volume

    def _reported_volume(self, p_id: str, raw_level: int) -> int:
        if self._zone_owns_volume(p_id):
            existing = self.store.state.players.get(p_id)
            return existing.volume if existing else 0
        return raw_level

    def _reported_mute(self, p_id: str, raw_muted: bool) -> bool:
        if self._zone_owns_volume(p_id):
            existing = self.store.state.players.get(p_id)
            return existing.muted if existing else False
        return raw_muted

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

    # -- queue and play mode (Phase 8) -----------------------------------------------------

    async def get_queue(
        self, player_id: str, limit: int = 200
    ) -> tuple[list[QueueEntry], int | None]:
        """``player/get_queue`` (pyheos ``get_queue(range_start, range_end)``, 0-based). HEOS
        returns at most 100 items per range, so the read pages up to ``limit``.

        The total is derived from paging: known when the last page came back short (the item
        count), ``None`` when it came back full because there may be more. Not because the
        protocol lacks a total -- ``player/get_queue`` reports ``count`` in its response message
        (CLI spec 4.2.15; observed ``count=674`` on an AVR-X3400H while this returned ``None``).
        ``pyheos.player_get_queue`` returns only ``result.payload`` and drops ``result.message``,
        so the count is not reachable through its public API. Fixing this properly means either a
        pyheos change or paging to exhaustion; do not "fix" it by reading ``heos._connection``."""
        raw = self._raw(player_id)
        get_queue = getattr(raw, "get_queue", None)
        if get_queue is None:
            raise UnsupportedCommandError("HEOS client cannot read the queue")
        items: list[Any] = []
        start = 0
        complete = False
        while len(items) < limit:
            end = min(start + 99, limit - 1)
            page = list(await get_queue(start, end))
            items.extend(page)
            if len(page) < (end - start + 1):
                complete = True
                break
            start = end + 1
        # Same trap as the queue-level source: a station playing over the top of a queue says
        # nothing about what is *in* the queue, so its source is not a usable fallback.
        media = getattr(raw, "now_playing_media", None)
        is_station = (getattr(media, "type", None) or "") == "station"
        source_id = None if is_station else getattr(media, "source_id", None)
        entries = [self._queue_entry(i, item, source_id) for i, item in enumerate(items[:limit])]
        self._queue_ids[player_id] = [_queue_id(item, i) for i, item in enumerate(items)]
        return entries, (len(items) if complete else None)

    def _queue_entry(self, index: int, item: Any, source_id: Any = None) -> QueueEntry:
        """A queue item does not name its source; the playing media's ``source_id`` is the best
        signal for which service the queue came from (never assume Tidal), but only when that
        media came from the queue -- a station is passed as ``None`` by the caller."""
        media_id = getattr(item, "media_id", None)
        sid = getattr(item, "source_id", None)
        sid = source_id if sid is None else sid
        return QueueEntry(
            index=index,
            title=getattr(item, "song", None),
            artist=getattr(item, "artist", None),
            album=getattr(item, "album", None),
            art=self.art.ref(
                ArtHints(
                    device_url=getattr(item, "image_url", None) or None,
                    service=_service_name(sid),
                )
            ),
            track_id=str(media_id) if media_id is not None else None,
            content_ref=_tidal_ref(sid, media_id),
        )

    async def play_queue_index(self, player_id: str, index: int) -> None:
        """``player/play_queue`` with the queue id captured by the last read (1-based on the
        receiver); re-reads the queue when the index is beyond what was cached."""
        raw = self._raw(player_id)
        play_queue = getattr(raw, "play_queue", None)
        if play_queue is None:
            raise UnsupportedCommandError("HEOS client cannot play a queue position")
        ids = self._queue_ids.get(player_id) or []
        if index >= len(ids):
            _entries, _total = await self.get_queue(player_id, limit=max(index + 1, 200))
            ids = self._queue_ids.get(player_id) or []
        if not 0 <= index < len(ids):
            raise IndexError(f"queue index {index} out of range for {len(ids)} items")
        await play_queue(ids[index])

    async def get_play_mode(self, player_id: str) -> PlayMode:
        raw = self._raw(player_id)
        return _play_mode_from_raw(raw)

    async def set_play_mode(self, player_id: str, mode: PlayMode) -> None:
        raw = self._raw(player_id)
        setter = getattr(raw, "set_play_mode", None)
        if setter is None:
            raise UnsupportedCommandError("HEOS client cannot set the play mode")
        from pyheos.types import RepeatType

        repeat = {"off": RepeatType.OFF, "one": RepeatType.ON_ONE, "all": RepeatType.ON_ALL}[
            mode.repeat
        ]
        await setter(repeat, mode.shuffle)
        self.store.set_play_mode(player_id, mode)

    def _schedule_queue_refresh(self, p_id: str) -> None:
        """Queue events arrive in bursts (one per added item); re-read once after a quiet gap."""
        side = self._side_id(raw_pid(p_id))
        if side is None:
            return
        pending = self._queue_refresh.get(side)
        if pending is not None and not pending.done():
            pending.cancel()
        task = self._spawn(self._refresh_queue(side, p_id))
        if task is not None:
            self._queue_refresh[side] = task

    async def _refresh_queue(self, side: str, p_id: str) -> None:
        await asyncio.sleep(QUEUE_DEBOUNCE_S)
        try:
            entries, total = await self.get_queue(p_id)
        except Exception as exc:  # noqa: BLE001 - a failed re-read leaves the last snapshot
            self.log.debug("queue re-read failed", extra={"extra": {"error": str(exc)}})
            return
        if side not in self.store.state.sides:
            return
        np = self.store.state.now_playing.get(side)
        self.store.set_queue(side, queue_state_for(entries, total, np))
        self._emit("queue", player_id=p_id)

    def _apply_now_playing(self, p: Player, raw: Any) -> None:
        media = getattr(raw, "now_playing_media", None)
        if media is None:
            return
        duration = getattr(media, "duration", None)
        controls = _controls(media)
        is_station = _enum_value(getattr(media, "type", "")) == "station"
        side = side_id_for(p)
        source_id = getattr(media, "source_id", None)
        service = _service_name(source_id)
        station_ref: ContentRef | None = None
        stored = self._station_playing.get(side)
        if stored is not None:
            station_ref, station_name = stored
            # The CLI has no station id on now-playing; ``station`` (or ``album``, where the
            # receiver puts the station name) is the best available switch detector.
            reported = getattr(media, "station", None) or getattr(media, "album", None)
            switched = bool(reported) and str(reported).casefold() != station_name.casefold()
            if service != station_ref.service or not is_station or switched:
                self._station_playing.pop(side, None)  # the room moved on
                station_ref = None
        self.store.set_now_playing(
            side,
            NowPlaying(
                art=self.art.ref(
                    ArtHints(
                        device_url=getattr(media, "image_url", None) or None,
                        service=_service_name(getattr(media, "source_id", None)),
                    )
                ),
                title=getattr(media, "song", None),
                artist=getattr(media, "artist", None),
                album=getattr(media, "album", None),
                source=service or (str(source_id) if source_id not in (None, "") else None),
                seekable=not is_station,  # the CLI cannot seek anyway; kept for the UI badge
                supports_next="play_next" in controls if controls else True,  # stations skip
                supports_prev="play_previous" in controls if controls else not is_station,
                duration_ms=int(duration) if duration else None,
                track_id=str(getattr(media, "media_id", "") or "") or None,
                content_ref=_tidal_ref(
                    getattr(media, "source_id", None), getattr(media, "media_id", None)
                )
                or station_ref,
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
            mapped = _play_state(getattr(raw, "state", None))
            if mapped is not None:  # transient "unknown": hold the known state
                self.store.update_player(p_id, play_state=mapped)
            self._emit("play_state", player_id=p_id)
        elif event == EVENT_PLAYER_VOLUME_CHANGED:
            self.store.update_player(
                p_id,
                volume=self._reported_volume(p_id, int(raw.volume)),
                muted=self._reported_mute(p_id, bool(getattr(raw, "is_muted", False))),
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
        elif event == EVENT_QUEUE_CHANGED:
            self._queue_ids.pop(p_id, None)  # the cached queue ids no longer match the receiver
            self._schedule_queue_refresh(p_id)
        elif event in (EVENT_REPEAT_MODE_CHANGED, EVENT_SHUFFLE_MODE_CHANGED):
            self.store.set_play_mode(p_id, _play_mode_from_raw(raw))
            self._emit("play_mode", player_id=p_id)

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


HEOS_SOURCE_NAMES: dict[int, str] = {1: "pandora", 4: "spotify", 10: "tidal", 13: "amazon"}


def _play_mode_from_raw(raw: Any) -> PlayMode:
    """pyheos ``HeosPlayer.repeat`` (RepeatType on_all|on_one|off) and ``shuffle`` (bool)."""
    repeat = _enum_value(getattr(raw, "repeat", "off")) or "off"
    mapped = {"on_all": "all", "on_one": "one", "off": "off"}.get(repeat, "off")
    return PlayMode(shuffle=bool(getattr(raw, "shuffle", False)), repeat=mapped)  # type: ignore[arg-type]


def _queue_id(item: Any, index: int) -> int:
    """HEOS queue ids are 1-based; prefer the receiver's own ``queue_id`` on the item."""
    qid = getattr(item, "queue_id", None)
    try:
        return int(qid) if qid is not None else index + 1
    except (TypeError, ValueError):
        return index + 1


def _tidal_ref(source_id: Any, media_id: Any) -> ContentRef | None:
    """Canonical track identity: the HEOS media id *is* the Tidal track id when the source is
    Tidal (sid 10). Anything else has no cross-vendor identity."""
    if _service_name(source_id) != "tidal" or not media_id:
        return None
    try:
        return ContentRef(service="tidal", kind="track", id=str(media_id))
    except ValueError:
        return None


def _service_name(source_id: Any) -> str | None:
    """Map a HEOS ``source_id`` to the hub's service badge names (Phase 3 extends this)."""
    try:
        return HEOS_SOURCE_NAMES.get(int(source_id)) if source_id is not None else None
    except (TypeError, ValueError):
        return None


def _is_station(item: Any) -> bool:
    return _enum_value(getattr(item, "type", "")) == "station" and bool(
        getattr(item, "playable", True)
    )


def _station_from(item: Any, sid: int, service: str) -> VendorStation:
    return VendorStation(
        vendor="heos",
        service=service,
        name=str(getattr(item, "name", "") or "Station"),
        ids={
            "sid": str(sid),
            "cid": str(getattr(item, "container_id", None) or ""),
            "mid": str(getattr(item, "media_id", None) or ""),
        },
        art_url=str(getattr(item, "image_url", "") or "") or None,
    )
