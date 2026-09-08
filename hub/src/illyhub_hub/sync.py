"""Sync Play: the same Tidal content on one HEOS side and one Sonos side, held together.

Design in ``docs/sync-engine.md``; API in ``docs/api.md`` → Sync Play. The two facts that shape
this module: the HEOS CLI cannot seek, so **HEOS is the clock master** and Sonos the only side
that is ever corrected; and both ecosystems report position at whole seconds, so drift is judged
on the hub's interpolated estimates (``HubState.positions`` extrapolated from ``reported_at``),
and only on confirmed ones (confidence above 0.5).

Track identity is never compared across vendors: each side is judged against *its own* primed
expectation, through the canonical ``now_playing.content_ref`` when the adapter supplies one and
title + artist otherwise.

One session at a time. Phases::

    resolving → priming → verifying → starting → locked ⇄ drifting → correcting → locked
                                                     ↘ lost (retry re-primes the follower)
                                                     ↘ stopped

Device work goes through the adapters directly (not the command router's ack machinery) so a
session does not spray acks; the engine emits exactly one ack per ``play`` / ``stop`` / ``retry``
call through the router's ack callbacks, and *listens* to the router's acks to mirror transport
sent to the master side.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import statistics
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .adapters.base import PlayableTrack, PlaybackAdapter, PrimedQueue
from .commands import Ack, CommandError, CommandRouter, ErrorEnvelope, resolve_sides
from .content import BrowseItem, ContentRef, NeedsLinkError
from .history import PlayHistory
from .logsetup import correlation_id, get_logger
from .state import NowPlaying, Side, StateStore, SyncState, SyncStatus
from .tasks import spawn, stop_task

log = get_logger("sync")

TracksFn = Callable[[ContentRef], Awaitable[tuple[BrowseItem, list[PlayableTrack]]]]
Target = str | list[str]

START_REPORT_WAIT_S = 6.0  # how long to wait for both sides' first confirmed report after fire
PLAY_GRACE_S = 6.0  # >= START_REPORT_WAIT_S: a side may still read "pause" this long after fire
TERMINAL: frozenset[str] = frozenset({"idle", "stopped", "lost"})
MIRRORED: frozenset[str] = frozenset({"play", "pause", "toggle", "stop", "next", "prev"})
SONOS_TIDAL_URI = re.compile(r"^x-sonos-http:track/(\d+)\.flac")


class SyncConfig(BaseModel):
    """Hot-reloadable tuning (``POST /api/sync/config``). Defaults come from settings."""

    drift_ms: int = Field(default=300, ge=50, le=5000)
    samples: int = Field(default=2, ge=1, le=20)
    correction_gap_s: float = Field(default=10.0, ge=0.1, le=120.0)
    lookahead_ms: int = Field(default=400, ge=0, le=5000)
    monitor_s: float = Field(default=0.5, ge=0.01, le=5.0)
    track_grace_s: float = Field(default=3.0, ge=0.1, le=30.0)


class SyncSessionSummary(BaseModel):
    id: str
    started_at: datetime
    ended_at: datetime | None = None
    content_ref: ContentRef
    title: str | None = None
    master_side: str
    follower_side: str
    status: str


class SyncReport(BaseModel):
    id: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_s: float
    samples: int
    start_delta_ms: int | None = None
    drift_mean_ms: float | None = None
    drift_p95_ms: float | None = None
    drift_max_ms: int | None = None
    corrections: int = 0
    reprimes: int = 0
    lost_reason: str | None = None


# ---------------------------------------------------------------------------------------
# Drift log (CSV per session + JSON sidecar with the summary)
# ---------------------------------------------------------------------------------------


class DriftLog:
    """Buffered ``t,master_pos,follower_pos,drift,action`` writer for one session."""

    FLUSH_EVERY = 20

    def __init__(self, directory: Path, session_id: str) -> None:
        self.directory = directory
        self.session_id = session_id
        self.path = directory / f"{session_id}.csv"
        self.meta_path = directory / f"{session_id}.json"
        self._rows: list[tuple[str, int | None, int | None, int | None, str]] = []
        self._header_written = False
        self._lock = asyncio.Lock()
        self.samples: list[int] = []  # |drift| per judged sample, for the report

    def add(
        self,
        t: datetime,
        master: int | None,
        follower: int | None,
        drift: int | None,
        action: str = "",
    ) -> None:
        self._rows.append((t.isoformat(), master, follower, drift, action))
        if drift is not None and not action:
            self.samples.append(abs(drift))

    def needs_flush(self) -> bool:
        return len(self._rows) >= self.FLUSH_EVERY

    async def flush(self) -> None:
        async with self._lock:
            if not self._rows:
                return
            rows, self._rows = self._rows, []
            write_header = not self._header_written
            self._header_written = True
            await asyncio.to_thread(self._write, rows, write_header)

    def _write(
        self, rows: list[tuple[str, int | None, int | None, int | None, str]], header: bool
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", newline="") as fh:
            writer = csv.writer(fh)
            if header:
                writer.writerow(["t", "master_pos", "follower_pos", "drift", "action"])
            writer.writerows(rows)

    async def write_meta(self, summary: SyncSessionSummary, report: SyncReport) -> None:
        payload = {
            "summary": summary.model_dump(mode="json"),
            "report": report.model_dump(mode="json"),
        }
        await asyncio.to_thread(self._write_meta, payload)

    def _write_meta(self, payload: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self.meta_path)


def report_from_samples(
    *,
    session_id: str,
    started_at: datetime,
    ended_at: datetime | None,
    samples: list[int],
    start_delta_ms: int | None,
    corrections: int,
    reprimes: int,
    lost_reason: str | None,
    now: datetime,
) -> SyncReport:
    end = ended_at or now
    p95: float | None = None
    if samples:
        ordered = sorted(samples)
        p95 = float(ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))])
    return SyncReport(
        id=session_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_s=round(max(0.0, (end - started_at).total_seconds()), 1),
        samples=len(samples),
        start_delta_ms=start_delta_ms,
        drift_mean_ms=round(statistics.fmean(samples), 1) if samples else None,
        drift_p95_ms=p95,
        drift_max_ms=max(samples) if samples else None,
        corrections=corrections,
        reprimes=reprimes,
        lost_reason=lost_reason,
    )


def trim_sessions(directory: Path, keep: int) -> list[str]:
    """Keep the newest ``keep`` sessions (by ``started_at`` in the sidecar); delete the rest."""
    if not directory.exists():
        return []
    metas: list[tuple[str, Path]] = []
    for meta in directory.glob("*.json"):
        try:
            started = json.loads(meta.read_text())["summary"]["started_at"]
        except (OSError, ValueError, KeyError):
            started = ""
        metas.append((started, meta))
    metas.sort(key=lambda m: m[0], reverse=True)
    removed: list[str] = []
    for _started, meta in metas[keep:]:
        for path in (meta, meta.with_suffix(".csv")):
            try:
                path.unlink()
            except OSError:
                continue
        removed.append(meta.stem)
    return removed


# ---------------------------------------------------------------------------------------
# Track identity
# ---------------------------------------------------------------------------------------


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def canonical_from_vendor_id(vendor: str, track_id: str | None) -> str | None:
    """Vendor-native track id → canonical Tidal id, when the namespace allows it. HEOS media ids
    *are* Tidal ids; Sonos ids are ``x-sonos-http:track/{id}.flac?…`` URIs. Never compare the raw
    ids of two vendors."""
    if not track_id:
        return None
    if vendor == "heos":
        return track_id if track_id.isdigit() else None
    if vendor == "sonos":
        m = SONOS_TIDAL_URI.match(track_id)
        return m.group(1) if m else None
    return None


def primed_matches(vendor: str, primed: PrimedQueue, expected: PlayableTrack) -> bool:
    """One side's primed description against *its own* expectation."""
    canonical = canonical_from_vendor_id(vendor, primed.track_id)
    if canonical is not None:
        return canonical == expected.track_id
    return _norm(primed.title) == _norm(expected.title)


def track_index(
    np: NowPlaying | None, tracks: list[PlayableTrack], near: int, service: str
) -> int | None:
    """Which index of ``tracks`` a side is on, from its now-playing. Canonical id first, then
    title + artist. Duplicates resolve to the index nearest the one we primed."""
    if np is None:
        return None
    ref = np.content_ref
    if ref is not None and ref.service == service and ref.kind == "track":
        hits = [i for i, t in enumerate(tracks) if t.track_id == ref.id]
        if hits:
            return min(hits, key=lambda i: abs(i - near))
    if np.title:
        hits = [
            i
            for i, t in enumerate(tracks)
            if _norm(t.title) == _norm(np.title)
            and (not t.artist or not np.artist or _norm(t.artist) == _norm(np.artist))
        ]
        if hits:
            return min(hits, key=lambda i: abs(i - near))
    return None


# ---------------------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------------------


@dataclass
class SyncSession:
    id: str
    generation: int
    ref: ContentRef
    item: BrowseItem
    tracks: list[PlayableTrack]
    index: int
    master: Side
    follower: Side
    log: DriftLog
    started_at: datetime
    status: SyncStatus = "resolving"
    ended_at: datetime | None = None
    lookahead_ms: float = 400.0
    corrections: int = 0
    reprimes: int = 0
    start_delta_ms: int | None = None
    last_correction_at: datetime | None = None
    reason: str | None = None
    drift_ms: int | None = None
    over: int = 0  # consecutive judged samples over threshold
    fired_at: datetime | None = None
    grace_until: datetime | None = None  # sides may still read "pause" until then
    seek_at: datetime | None = None  # a follower seek awaiting its confirming report
    boundary_deadline: datetime | None = None
    boundary_kind: str | None = None  # master_first | follower_first | mismatch
    busy: int = 0  # engine is driving a side; suppress "lost" detection while > 0
    paused: bool = False  # both sides paused through the hub; not "lost"
    settle_armed: bool = False  # one gap-exempt follow-up correction is available
    settle_used: bool = False  # ...and it has been spent for this over-threshold episode
    last_master_pos: int | None = None
    expected: dict[str, PrimedQueue] = field(default_factory=dict)  # side id -> primed
    snapshots: dict[str, Any] = field(default_factory=dict)  # side id -> queue snapshot
    created_groups: list[str] = field(default_factory=list)  # side ids grouped for this session
    targets: list[str] = field(default_factory=list)

    def summary(self) -> SyncSessionSummary:
        return SyncSessionSummary(
            id=self.id,
            started_at=self.started_at,
            ended_at=self.ended_at,
            content_ref=self.ref,
            title=self.item.title,
            master_side=self.master.id,
            follower_side=self.follower.id,
            status=self.status,
        )

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL


class SyncEngine:
    """One Sync Play session at a time. ``play`` resolves, primes, verifies and fires, then hands
    the session to a monitor task that holds the follower to the master."""

    def __init__(
        self,
        store: StateStore,
        router: CommandRouter,
        *,
        tracks_for: TracksFn,
        history: PlayHistory | None = None,
        config: SyncConfig | None = None,
        log_dir: Path,
        max_sessions: int = 50,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.router = router
        self._tracks_for = tracks_for
        self.history = history
        self.config = config or SyncConfig()
        self.log_dir = log_dir
        self.max_sessions = max_sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self.session: SyncSession | None = None
        self._generation = 0
        self._monitor: asyncio.Task[None] | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()
        self._mirror_queue: asyncio.Queue[tuple[SyncSession, str, bool]] = asyncio.Queue()
        self._mirror_worker: asyncio.Task[None] | None = None
        self._unsubscribe_acks = router.on_ack(self._on_ack)

    # -- public ---------------------------------------------------------------------

    def current(self) -> SyncState:
        return self.store.state.sync

    async def play(
        self, ref: ContentRef, heos_target: Target, sonos_target: Target, start_index: int = 0
    ) -> Ack:
        async with self._lock:
            await self._end_current("superseded by a new Sync Play")
            self._generation += 1
            generation = self._generation
            self.store.set_sync(SyncState(status="resolving", content_ref=ref))
            task = spawn(
                self._resolve_prime_start(generation, ref, heos_target, sonos_target, start_index),
                self._tasks,
            )
            assert task is not None
            self._start_task = task
        failed = await self._await_start(task, generation, "sync_play")
        if failed is not None:
            return failed
        session = self.session
        assert session is not None and session.generation == generation
        if self.history is not None:
            await self.history.record(
                ref,
                title=session.item.title,
                subtitle=session.item.subtitle,
                art=session.item.art,
                targets=session.targets,
                side_ids=[session.master.id, session.follower.id],
                sync=True,
            )
        return self._ack(
            "sync_play",
            applied=[session.master.id, session.follower.id],
            session_id=session.id,
        )

    async def stop(self) -> Ack:
        async with self._lock:
            session = self.session
            starting = self._start_task is not None and not self._start_task.done()
            if not starting and (session is None or session.status in ("idle", "stopped")):
                return self._ack(
                    "sync_stop",
                    error=CommandError("sync_idle", "Nothing is sync playing.", "sync"),
                )
            await self._cancel_start()
            session = self.session  # a cancelled resolve may not have produced one
            if session is None or session.status == "stopped":
                self.store.set_sync(SyncState(status="stopped", reason="stopped by the user"))
                return self._ack("sync_stop")
            await self._end_session(session, "stopped", "stopped by the user", action="stop")
            return self._ack(
                "sync_stop", applied=[session.master.id, session.follower.id], session_id=session.id
            )

    async def retry(self) -> Ack:
        """Recover a **lost** session: re-prime the follower from the master's current track; if
        the master is not playing, both restart from that track. A stopped session is not
        retried (``sync_stopped``): the user let go on purpose."""
        async with self._lock:
            session = self.session
            if session is None or session.status == "idle":
                return self._ack(
                    "sync_retry",
                    error=CommandError("sync_idle", "There is nothing to retry.", "sync"),
                )
            if session.status == "stopped":
                return self._ack(
                    "sync_retry",
                    error=CommandError(
                        "sync_stopped", "Sync Play was stopped; start it again.", "sync"
                    ),
                    session_id=session.id,
                )
            if session.status != "lost":
                return self._ack(
                    "sync_retry",
                    error=CommandError("invalid_argument", "Sync Play is running.", "sync"),
                    session_id=session.id,
                )
            await self._cancel_start()
            await stop_task(self._monitor)
            self._monitor = None
            state = self.store.state
            master = state.sides.get(session.master.id)
            follower = state.sides.get(session.follower.id)
            if master is None or follower is None:
                exc = CommandError("unknown_target", "A synced room is gone.", "sync")
                await self._lose(session, exc.message)
                return self._ack("sync_retry", error=exc, session_id=session.id)
            session.master, session.follower = master, follower
            session.index = self._master_index(session)
            session.ended_at = None
            session.reason = None
            session.paused = False
            session.boundary_deadline = session.boundary_kind = None
            session.seek_at = None
            generation = session.generation
            task = self._spawn_start(session, both=master.play_state != "play")
        failed = await self._await_start(task, generation, "sync_retry")
        if failed is not None:
            return failed
        return self._ack("sync_retry", applied=[master.id, follower.id], session_id=session.id)

    async def update_config(self, **changes: Any) -> SyncConfig:
        candidate = SyncConfig.model_validate({**self.config.model_dump(), **changes})
        self.config = candidate
        return self.config

    async def aclose(self) -> None:
        self._unsubscribe_acks()
        await self._cancel_start()
        session = self.session
        if session is not None and not session.terminal:
            await self._end_session(session, "stopped", "hub shutting down", action="stop")
        await stop_task(self._monitor)
        self._monitor = None
        await stop_task(self._mirror_worker)
        self._mirror_worker = None
        for t in list(self._tasks):
            await stop_task(t)
        self._tasks.clear()

    # -- sessions on disk -----------------------------------------------------------

    async def sessions(self, limit: int = 50) -> list[SyncSessionSummary]:
        def read() -> list[SyncSessionSummary]:
            out: list[SyncSessionSummary] = []
            if not self.log_dir.exists():
                return out
            for meta in self.log_dir.glob("*.json"):
                try:
                    payload = json.loads(meta.read_text())
                    out.append(SyncSessionSummary.model_validate(payload["summary"]))
                except (OSError, ValueError, KeyError):
                    continue
            out.sort(key=lambda s: s.started_at, reverse=True)
            return out[: max(1, limit)]

        return await asyncio.to_thread(read)

    async def report(self, session_id: str) -> SyncReport | None:
        live = self.session
        if live is not None and live.id == session_id:
            await live.log.flush()
            return self._report(live)

        def read() -> SyncReport | None:
            meta = self.log_dir / f"{session_id}.json"
            if not meta.exists():
                return None
            try:
                return SyncReport.model_validate(json.loads(meta.read_text())["report"])
            except (OSError, ValueError, KeyError):
                return None

        return await asyncio.to_thread(read)

    # -- start pipeline -----------------------------------------------------------

    async def _resolve_prime_start(
        self,
        generation: int,
        ref: ContentRef,
        heos_target: Target,
        sonos_target: Target,
        start_index: int,
    ) -> None:
        session = await self._resolve(generation, ref, heos_target, sonos_target, start_index)
        self.session = session
        await self._prime_and_start(session, both=True)

    def _spawn_start(self, session: SyncSession, *, both: bool) -> asyncio.Task[None]:
        """Prime/verify/start runs as its own task so ``stop`` can cancel it mid-phase."""
        task = spawn(self._prime_and_start(session, both=both), self._tasks)
        assert task is not None
        self._start_task = task
        return task

    async def _await_start(
        self, task: asyncio.Task[None], generation: int, action: str
    ) -> Ack | None:
        """Wait for a start task. Returns an error ack when it failed or was stopped, else None
        (and the monitor is running). ``generation`` guards against a superseded start."""
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            exc = CommandError("sync_stopped", "Sync Play was stopped.", "sync")
            session = self.session
            sid = session.id if session is not None and session.generation == generation else None
            return self._ack(action, error=exc, session_id=sid)
        except CommandError as exc:
            session = self.session
            if session is None or session.generation != generation or session.status == "resolving":
                # Nothing started: the chip hides (idle); the toast carries the reason.
                if session is None or session.generation == generation:
                    self.store.set_sync(SyncState(status="idle", reason=exc.message))
                return self._ack(action, error=exc)
            await self._lose(session, exc.message)
            return self._ack(action, error=exc, session_id=session.id)
        finally:
            if self._start_task is task:
                self._start_task = None
        session = self.session
        if session is None or session.generation != generation:
            exc = CommandError("sync_stopped", "Sync Play was superseded.", "sync")
            return self._ack(action, error=exc)
        await self._start_monitor()
        return None

    async def _resolve(
        self,
        generation: int,
        ref: ContentRef,
        heos_target: Target,
        sonos_target: Target,
        start_index: int,
    ) -> SyncSession:
        if ref.service != "tidal":
            raise CommandError(
                "unsupported_content", "Sync Play works with Tidal albums and playlists.", "sync"
            )
        try:
            item, tracks = await self._tracks_for(ref)
        except NeedsLinkError as exc:
            raise CommandError("needs_link", exc.message, "sync") from exc
        if not tracks:
            raise CommandError("invalid_argument", "There is nothing to play.", "sync")
        if not 0 <= start_index < len(tracks):
            raise CommandError("invalid_argument", "Start index is out of range.", "sync")
        created: list[str] = []
        try:
            master, made = await self._resolve_side("heos", heos_target)
            if made:
                created.append(master.id)
            follower, made = await self._resolve_side("sonos", sonos_target)
            if made:
                created.append(follower.id)
            avail = self.router.service_availability(ref.service)
            for side in (master, follower):
                if not getattr(avail, side.vendor, False):
                    raise CommandError(
                        "not_available_on_side",
                        f"{side.name} can't play Tidal; link it in the {side.vendor} app.",
                        side.id,
                    )
        except CommandError:
            for side_id in created:  # undo groups we made for a session that will not start
                await self.router.ungroup(side_id)
            raise
        now = self._clock()
        session_id = f"{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:4]}"
        targets = [
            *(heos_target if isinstance(heos_target, list) else [heos_target]),
            *(sonos_target if isinstance(sonos_target, list) else [sonos_target]),
        ]
        return SyncSession(
            id=session_id,
            generation=generation,
            ref=ref,
            item=item,
            tracks=tracks,
            index=start_index,
            master=master,
            follower=follower,
            log=DriftLog(self.log_dir, session_id),
            started_at=now,
            lookahead_ms=float(self.config.lookahead_ms),
            created_groups=created,
            targets=targets,
        )

    async def _resolve_side(self, vendor: str, target: Target) -> tuple[Side, bool]:
        """A player id, a side id, or a list of player ids (grouped natively first). Returns the
        side and whether the engine created a group for it."""
        created = False
        if isinstance(target, list):
            if not target:
                raise CommandError("invalid_argument", f"No {vendor} room was chosen.", vendor)
            if len(target) > 1:
                ack = await self.router.group(vendor, target[0], target[1:])  # type: ignore[arg-type]
                if not ack.ok and ack.error is not None:
                    raise CommandError(ack.error.code, ack.error.message, ack.error.target)
                created = True
            target = target[0]
        sides = resolve_sides(self.store.state, target)
        if target == "all" or len(sides) != 1:
            raise CommandError(
                "invalid_argument", f"Sync Play needs exactly one {vendor} room.", target
            )
        side = sides[0]
        if side.vendor != vendor:
            raise CommandError("invalid_argument", f"{side.name} is not a {vendor} room.", target)
        return side, created

    def _adapter(self, side: Side) -> PlaybackAdapter:
        return self.router._adapter_for(side.vendor, side.id)  # noqa: SLF001 - same package

    async def _prime_and_start(self, session: SyncSession, *, both: bool) -> None:
        """Prime (both sides in parallel, or just the follower), verify, fire. The ack returns
        at ``starting``; the first judged sample moves the session to locked/drifting and the
        start delta lands from a background measurement. Raises CommandError."""
        session.busy += 1
        try:
            self._publish(session, "priming")
            master_ad, follower_ad = self._adapter(session.master), self._adapter(session.follower)
            if both:
                snap = await self._snapshot(follower_ad, session.follower)
                if snap is not None:
                    session.snapshots[session.follower.id] = snap
                await self._prime_both(session, master_ad, follower_ad)
                self._publish(session, "verifying")
                self._verify(session)
            else:
                primed = await self._timed(
                    session.follower,
                    follower_ad.prime_content(
                        session.follower.coordinator_player_id,
                        session.ref,
                        session.tracks,
                        session.index,
                    ),
                    "prime",
                    kind="prime",
                )
                session.expected[session.follower.id] = primed
                expected = session.tracks[session.index]
                if not primed_matches(session.follower.vendor, primed, expected):
                    raise CommandError(
                        "sync_mismatch",
                        f"{session.follower.name} loaded a different track.",
                        session.follower.id,
                    )
            self._publish(session, "starting")
            now = self._clock()
            session.fired_at = now
            session.grace_until = now + timedelta(seconds=PLAY_GRACE_S)
            session.seek_at = None
            session.over = 0
            session.settle_armed = session.settle_used = False
            session.boundary_deadline = session.boundary_kind = None
            session.drift_ms = None
            if both:
                await self._fire_both(session, master_ad, follower_ad)
                spawn(self._measure_start(session), self._tasks)
            else:
                await self._timed(
                    session.follower,
                    follower_ad.start_primed(session.follower.coordinator_player_id, session.index),
                    "play",
                )
            self._publish(session, "starting")
        finally:
            session.busy -= 1

    async def _snapshot(self, adapter: PlaybackAdapter, side: Side) -> Any | None:
        try:
            return await adapter.snapshot_queue(side.coordinator_player_id)
        except Exception as exc:  # noqa: BLE001 - a missing snapshot only weakens the rollback
            log.warning(
                "queue snapshot failed", extra={"extra": {"side": side.id, "error": str(exc)}}
            )
            return None

    async def _prime_both(
        self, session: SyncSession, master_ad: PlaybackAdapter, follower_ad: PlaybackAdapter
    ) -> None:
        """Prime both sides in parallel. If one fails after the other has already replaced its
        queue, say so, and put the Sonos queue back when a snapshot exists."""
        sides = (session.master, session.follower)
        adapters = (master_ad, follower_ad)
        results = await asyncio.gather(
            *(
                self._timed(
                    side,
                    ad.prime_content(
                        side.coordinator_player_id, session.ref, session.tracks, session.index
                    ),
                    "prime",
                    kind="prime",
                )
                for side, ad in zip(sides, adapters, strict=True)
            ),
            return_exceptions=True,
        )
        failures = [
            (s, r) for s, r in zip(sides, results, strict=True) if isinstance(r, BaseException)
        ]
        if not failures:
            for side, primed in zip(sides, results, strict=True):
                session.expected[side.id] = primed
            return
        for side, result in zip(sides, results, strict=True):
            if isinstance(result, BaseException):
                continue
            # This side's queue was rebuilt for a start that will not happen.
            snap = session.snapshots.pop(side.id, None)
            restored = False
            if snap is not None:
                try:
                    await adapters[sides.index(side)].restore_queue(
                        side.coordinator_player_id, snap
                    )
                    restored = True
                except Exception as exc:  # noqa: BLE001
                    log.warning("queue restore failed", extra={"extra": {"error": str(exc)}})
            rebuilt = (
                f"{side.name}'s queue was {'restored' if restored else 'rebuilt and left paused'}"
            )
            break
        else:  # pragma: no cover - both failed: nothing to report about a rebuilt queue
            rebuilt = ""
        failed_side, err = failures[0]
        if isinstance(err, asyncio.CancelledError):
            raise err
        message = f"{failed_side.name} didn't prime for Sync Play"
        if rebuilt:
            message = f"{message}; {rebuilt}."
        else:
            message = f"{message}."
        code = err.code if isinstance(err, CommandError) else "vendor_error"
        raise CommandError(code, message, failed_side.id) from err

    async def _timed(
        self, side: Side, coro: Awaitable[Any], verb: str, *, kind: str = "transport"
    ) -> Any:
        """Run one adapter call, feed the router's latency tracker under ``kind``, map failures."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            result = await coro
        except CommandError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - vendor libraries raise their own types
            log.warning(
                "sync adapter call failed",
                extra={"extra": {"verb": verb, "side": side.id, "error": str(exc)}},
            )
            raise CommandError(
                "vendor_error", f"{side.name} didn't {verb} for Sync Play.", side.id
            ) from exc
        self.router.latency.record(side.vendor, (loop.time() - started) * 1000, kind)
        return result

    def _verify(self, session: SyncSession) -> None:
        """Each side against its own primed expectation; never one vendor's id against another's."""
        expected = session.tracks[session.index]
        for side in (session.master, session.follower):
            primed = session.expected.get(side.id)
            if primed is None or not primed_matches(side.vendor, primed, expected):
                raise CommandError(
                    "sync_mismatch",
                    f"{side.name} loaded a different track from the one Sync Play expected.",
                    side.id,
                )

    async def _fire_both(
        self, session: SyncSession, master_ad: PlaybackAdapter, follower_ad: PlaybackAdapter
    ) -> None:
        """Play goes to the slower vendor first, offset by the transport round-trip difference.
        Prime and seek durations never enter that estimate."""
        lat = self.router.latency
        m_ms = lat.average(session.master.vendor, 100.0)
        f_ms = lat.average(session.follower.vendor, 100.0)
        first, second = (
            ((session.master, master_ad), (session.follower, follower_ad))
            if m_ms >= f_ms
            else ((session.follower, follower_ad), (session.master, master_ad))
        )
        offset_s = abs(m_ms - f_ms) / 1000.0
        await self._timed(
            first[0], first[1].start_primed(first[0].coordinator_player_id, session.index), "play"
        )
        if offset_s > 0.005:
            await asyncio.sleep(min(offset_s, 2.0))
        await self._timed(
            second[0],
            second[1].start_primed(second[0].coordinator_player_id, session.index),
            "play",
        )
        log.info(
            "sync fired",
            extra={
                "extra": {
                    "session": session.id,
                    "first": first[0].id,
                    "offset_ms": round(offset_s * 1000),
                }
            },
        )

    async def _measure_start(self, session: SyncSession) -> None:
        """Start delta = follower start instant − master start instant, from each side's first
        confirmed position report after fire (start instant = reported_at − position)."""
        fired = session.fired_at
        assert fired is not None
        deadline = asyncio.get_running_loop().time() + START_REPORT_WAIT_S
        starts: dict[str, datetime] = {}
        while len(starts) < 2 and asyncio.get_running_loop().time() < deadline:
            if self.session is not session:
                return
            for side in (session.master, session.follower):
                if side.id in starts:
                    continue
                pos = self.store.state.positions.get(side.id)
                if pos is not None and pos.reported_at > fired and pos.confidence > 0.5:
                    starts[side.id] = pos.reported_at - timedelta(milliseconds=pos.position_ms)
            if len(starts) < 2:
                await asyncio.sleep(min(0.05, self.config.monitor_s))
        if len(starts) == 2 and self.session is session and not session.terminal:
            delta = starts[session.follower.id] - starts[session.master.id]
            session.start_delta_ms = int(round(delta.total_seconds() * 1000))
            log.info(
                "sync started",
                extra={"extra": {"session": session.id, "start_delta_ms": session.start_delta_ms}},
            )
            self._publish(session, session.status)

    # -- monitor --------------------------------------------------------------------

    async def _start_monitor(self) -> None:
        await stop_task(self._monitor)
        self._monitor = spawn(self._monitor_loop(), self._tasks)

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.monitor_s)
            session = self.session
            if session is None or session.terminal:
                return
            try:
                await self._sample(session)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the monitor must survive a bad sample
                log.exception("sync monitor sample failed")
            if session.log.needs_flush():
                await session.log.flush()

    async def _sample(self, session: SyncSession) -> None:
        state = self.store.state
        now = self._clock()
        master = state.sides.get(session.master.id)
        follower = state.sides.get(session.follower.id)
        if master is None or follower is None:
            await self._lose(session, "a synced room is gone")
            return
        session.master, session.follower = master, follower
        for side in (master, follower):
            conn = state.connections.get(side.vendor)
            if conn is None or conn.state != "connected":
                await self._lose(session, f"the {side.vendor} link dropped")
                return
        if session.paused:
            return  # both parked through the hub; nothing to judge until play resumes
        in_grace = session.grace_until is not None and now < session.grace_until
        if in_grace and master.play_state == follower.play_state == "play":
            session.grace_until = None  # both sides are audibly playing: the grace has done its job
            in_grace = False
        if session.busy == 0 and not in_grace:
            if master.play_state != "play":
                if self._ended_naturally(session, master):
                    await self._end_session(session, "stopped", "playback ended", action="stop")
                else:
                    await self._lose(session, f"{master.name} stopped playing")
                return
            if follower.play_state != "play" and session.boundary_deadline is None:
                await self._lose(session, f"{follower.name} stopped playing")
                return

        # Where is each side? Judged per side through its own identity, never across vendors.
        m_idx = track_index(
            state.now_playing.get(master.id), session.tracks, session.index, session.ref.service
        )
        f_idx = track_index(
            state.now_playing.get(follower.id), session.tracks, session.index, session.ref.service
        )
        if await self._handle_boundary(session, m_idx, f_idx, now):
            return

        # A correction that never gets a confirming report is time-boxed at twice the gap, so it
        # cannot block judgement forever (checked before the confidence gate, which is exactly
        # what an unconfirmed seek leaves behind).
        if (
            session.seek_at is not None
            and (now - session.seek_at).total_seconds() > 2 * self.config.correction_gap_s
        ):
            session.seek_at = None  # keep the lookahead: nothing was measured
            session.over = 0
            log.warning("sync correction never confirmed", extra={"extra": {"session": session.id}})

        # Confidence gate: whole-second reports right after a seek or track change are only
        # estimates (0.5). Judging on them would correct against noise.
        m_rec = state.positions.get(master.id)
        f_rec = state.positions.get(follower.id)
        if m_rec is None or f_rec is None or m_rec.confidence <= 0.5 or f_rec.confidence <= 0.5:
            return
        m_pos = self._position(master, now)
        f_pos = self._position(follower, now)
        if m_pos is None or f_pos is None:
            return
        session.last_master_pos = m_pos
        drift = f_pos - m_pos
        session.drift_ms = drift

        # A correction in flight: wait for the follower's first confirmed report after the seek.
        # The residual then tells the true seek latency: we seeked to master + L, so after landing
        # drift == L − latency, hence latency == L − residual. That becomes the next lookahead
        # and arms one gap-exempt follow-up correction per over-threshold episode.
        if session.seek_at is not None:
            if f_rec.reported_at > session.seek_at:
                measured = session.lookahead_ms - drift
                session.lookahead_ms = max(0.0, min(5000.0, measured))
                session.seek_at = None
                session.over = 0
                if not session.settle_used:
                    session.settle_armed = True
                log.info(
                    "sync correction landed",
                    extra={
                        "extra": {
                            "session": session.id,
                            "residual_ms": drift,
                            "lookahead_ms": round(session.lookahead_ms),
                        }
                    },
                )
            elif (now - session.seek_at).total_seconds() > 2 * self.config.correction_gap_s:
                session.seek_at = None  # time-boxed: no confirming report; keep the lookahead
                session.over = 0
                log.warning(
                    "sync correction never confirmed", extra={"extra": {"session": session.id}}
                )
            else:
                session.log.add(now, m_pos, f_pos, drift)
                self._publish(session, "correcting")
                return

        over = abs(drift) > self.config.drift_ms
        session.over = session.over + 1 if over else 0
        if not over:
            session.settle_armed = session.settle_used = False  # the episode is over
        gap_ok = (
            session.last_correction_at is None
            or (now - session.last_correction_at).total_seconds() >= self.config.correction_gap_s
        )
        if over and session.settle_armed:
            session.settle_used = True
            await self._correct(session, m_pos, now)
            return
        if over and session.over >= self.config.samples and gap_ok:
            await self._correct(session, m_pos, now)
            return
        session.log.add(now, m_pos, f_pos, drift)
        self._publish(session, "drifting" if over else "locked")

    def _ended_naturally(self, session: SyncSession, master: Side) -> bool:
        """Master stopped on the last track within a second of its end: the album finished."""
        if session.index != len(session.tracks) - 1 or session.last_master_pos is None:
            return False
        np = self.store.state.now_playing.get(master.id)
        duration = np.duration_ms if np else None
        return bool(duration) and session.last_master_pos >= (duration or 0) - 1000

    async def _handle_boundary(
        self, session: SyncSession, m_idx: int | None, f_idx: int | None, now: datetime
    ) -> bool:
        """Track boundaries. Returns True when the sample should not judge drift.

        - both on the primed index: nothing to do;
        - master moved to a new index: the follower must show it within ``track_grace_s``
          (master_first), else the follower is re-primed there;
        - follower moved to ``index + 1`` first: wait for the master (follower_first); the
          follower is **never** seeked backwards into the previous track; if the master does not
          follow within the grace, the follower is re-primed at the master's track;
        - anything else: mismatch; re-prime the follower at the master's track after the grace.
        """
        expected = session.index
        if m_idx is not None and m_idx != expected:
            if f_idx == m_idx:
                session.index = m_idx
                self._clear_boundary(session, now, "track")
                return False
            return await self._boundary_wait(session, "master_first", m_idx, now)
        if f_idx is not None and f_idx != expected:
            kind = "follower_first" if f_idx == expected + 1 else "mismatch"
            return await self._boundary_wait(session, kind, expected, now)
        if session.boundary_kind is not None:
            self._clear_boundary(session, now, "track")
        return False

    async def _boundary_wait(
        self, session: SyncSession, kind: str, reprime_at: int, now: datetime
    ) -> bool:
        if session.boundary_kind != kind:
            session.boundary_kind = kind
            session.boundary_deadline = now + timedelta(seconds=self.config.track_grace_s)
            session.seek_at = None  # a correction cannot be confirmed across tracks
            session.over = 0
            log.info(
                "sync track boundary",
                extra={"extra": {"session": session.id, "kind": kind, "index": reprime_at}},
            )
        assert session.boundary_deadline is not None
        if now >= session.boundary_deadline:
            session.index = reprime_at
            session.boundary_kind = session.boundary_deadline = None
            await self._reprime_follower(session)
        return True

    def _clear_boundary(self, session: SyncSession, now: datetime, action: str) -> None:
        session.boundary_kind = session.boundary_deadline = None
        session.seek_at = None
        session.over = 0
        session.settle_armed = session.settle_used = False
        session.log.add(now, None, None, None, action)

    async def _correct(self, session: SyncSession, master_pos: int, now: datetime) -> None:
        target = master_pos + int(session.lookahead_ms)
        adapter = self._adapter(session.follower)
        session.settle_armed = False
        session.busy += 1
        try:
            await self._timed(
                session.follower,
                adapter.seek(session.follower.coordinator_player_id, target),
                "seek",
                kind="seek",
            )
        except CommandError as exc:
            await self._lose(session, exc.message)
            return
        finally:
            session.busy -= 1
        session.seek_at = self._clock()
        session.last_correction_at = session.seek_at
        session.corrections += 1
        session.over = 0
        session.log.add(now, master_pos, None, session.drift_ms, "correct")
        log.info(
            "sync correction",
            extra={
                "extra": {
                    "session": session.id,
                    "drift_ms": session.drift_ms,
                    "target_ms": target,
                    "n": session.corrections,
                }
            },
        )
        self._publish(session, "correcting")

    async def _reprime_follower(self, session: SyncSession) -> None:
        session.reprimes += 1
        session.log.add(self._clock(), None, None, None, "reprime")
        try:
            await self._prime_and_start(session, both=False)
        except CommandError as exc:
            await self._lose(session, exc.message)

    def _master_index(self, session: SyncSession) -> int:
        """The master's current index, resolved through its own identity, nearest the primed one."""
        idx = track_index(
            self.store.state.now_playing.get(session.master.id),
            session.tracks,
            session.index,
            session.ref.service,
        )
        return session.index if idx is None else idx

    def _position(self, side: Side, now: datetime) -> int | None:
        pos = self.store.state.positions.get(side.id)
        if pos is None:
            return None
        if side.play_state != "play":
            return pos.position_ms
        elapsed = (now - pos.reported_at).total_seconds()
        return pos.position_ms + max(0, int(elapsed * 1000))

    # -- transport mirroring --------------------------------------------------------

    def _on_ack(self, ack: Ack) -> None:
        """Router acks drive two things: transport on the master side is mirrored to the follower
        (serialised through one worker), and a new play on either synced side ends the session."""
        session = self.session
        if session is None or session.terminal or not ack.ok:
            return
        synced = {session.master.id, session.follower.id}
        if ack.action == "play_content" and synced & set(ack.applied):
            spawn(
                self._end_session(session, "stopped", "playback changed", action="stop"),
                self._tasks,
            )
            return
        if ack.action not in MIRRORED:
            return
        if session.master.id in ack.applied:
            resolved = ack.resolved.get(session.master.id, ack.action)
            if resolved == "toggle":  # an older router without resolved actions
                return
            self._mirror_queue.put_nowait((session, resolved, session.follower.id in ack.applied))
            if self._mirror_worker is None or self._mirror_worker.done():
                self._mirror_worker = spawn(self._mirror_loop(), self._tasks)
        elif session.follower.id in ack.applied:
            resolved = ack.resolved.get(session.follower.id, ack.action)
            if resolved in ("pause", "stop"):
                spawn(
                    self._lose(session, f"{session.follower.name} was paused from the app"),
                    self._tasks,
                )

    async def _mirror_loop(self) -> None:
        while True:
            session, action, already = await self._mirror_queue.get()
            try:
                if session is not self.session or session.terminal:
                    continue
                await self._mirror(session, action, already)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the worker must survive a bad command
                log.exception("sync mirror failed")
            finally:
                self._mirror_queue.task_done()

    async def _mirror(self, session: SyncSession, action: str, already: bool) -> None:
        follower = self.store.state.sides.get(session.follower.id)
        master = self.store.state.sides.get(session.master.id)
        if follower is None or master is None:
            return
        try:
            adapter = self._adapter(follower)
            coord = follower.coordinator_player_id
            if action == "stop":
                if not already:
                    await self._timed(follower, adapter.stop(coord), "stop")
                await self._end_session(
                    session, "stopped", f"{master.name} was stopped", action="stop"
                )
                return
            session.busy += 1
            try:
                if action == "pause":
                    if not already:
                        await self._timed(follower, adapter.pause(coord), "pause")
                    session.paused = True
                elif action == "play":
                    if not already:
                        await self._timed(follower, adapter.play(coord), "play")
                    session.paused = False
                    session.grace_until = self._clock() + timedelta(seconds=PLAY_GRACE_S)
                    self._allow_immediate_correction(session)
                elif action == "next":
                    if not already:
                        await self._timed(follower, adapter.next(coord), "next")
                    self._allow_immediate_correction(session)
                elif action == "prev":
                    if not already:
                        await self._timed(follower, adapter.previous(coord), "previous")
                    self._allow_immediate_correction(session)
            finally:
                session.busy -= 1
            log.info(
                "sync mirrored",
                extra={"extra": {"session": session.id, "action": action, "already": already}},
            )
        except CommandError as exc:
            await self._lose(session, exc.message)

    @staticmethod
    def _allow_immediate_correction(session: SyncSession) -> None:
        """After a resume or track jump both sides restart their clocks; let the first
        over-threshold sample correct without waiting out the gap."""
        session.last_correction_at = None
        session.over = 0
        session.seek_at = None
        session.settle_armed = session.settle_used = False

    # -- ending ---------------------------------------------------------------------

    async def _lose(self, session: SyncSession, reason: str) -> None:
        await self._end_session(session, "lost", reason, action="lost")

    async def _cancel_start(self) -> None:
        task = self._start_task
        self._start_task = None
        if task is not None and not task.done():
            await stop_task(task)

    async def _end_current(self, reason: str) -> None:
        await self._cancel_start()
        await stop_task(self._monitor)
        self._monitor = None
        session = self.session
        if session is not None and not session.terminal:
            await self._end_session(session, "stopped", reason, action="stop")

    async def _end_session(
        self, session: SyncSession, status: SyncStatus, reason: str, *, action: str
    ) -> None:
        if session.terminal and session.ended_at is not None:
            return
        now = self._clock()
        session.ended_at = now
        session.reason = reason
        session.log.add(now, None, None, None, action)
        self._publish(session, status)
        try:
            await session.log.flush()
            await session.log.write_meta(session.summary(), self._report(session))
            await asyncio.to_thread(trim_sessions, self.log_dir, self.max_sessions)
        except OSError:
            log.exception("sync log write failed", extra={"extra": {"session": session.id}})
        if asyncio.current_task() is not self._monitor:
            await stop_task(self._monitor)
            self._monitor = None
        log.info(
            "sync ended",
            extra={"extra": {"session": session.id, "status": status, "reason": reason}},
        )

    def _report(self, session: SyncSession) -> SyncReport:
        return report_from_samples(
            session_id=session.id,
            started_at=session.started_at,
            ended_at=session.ended_at,
            samples=session.log.samples,
            start_delta_ms=session.start_delta_ms,
            corrections=session.corrections,
            reprimes=session.reprimes,
            lost_reason=session.reason if session.status == "lost" else None,
            now=self._clock(),
        )

    # -- state publishing -----------------------------------------------------------

    def _publish(self, session: SyncSession, status: SyncStatus) -> None:
        """Publish a session's state. A superseded session (a stale start task, a late
        measurement) never overwrites the live one."""
        session.status = status
        if self.session is not None and self.session is not session:
            return
        self.store.set_sync(
            SyncState(
                status=status,
                session_id=session.id,
                master_side=session.master.id,
                follower_side=session.follower.id,
                content_ref=session.ref,
                title=session.item.title,
                drift_ms=session.drift_ms,
                start_delta_ms=session.start_delta_ms,
                last_correction_at=session.last_correction_at,
                corrections=session.corrections,
                reason=session.reason,
                started_at=session.started_at,
            )
        )

    # -- acks -----------------------------------------------------------------------

    def _ack(
        self,
        action: str,
        *,
        error: CommandError | None = None,
        applied: list[str] | None = None,
        session_id: str | None = None,
    ) -> Ack:
        cid = correlation_id.get() or "-"
        ack = Ack(
            correlation_id=cid,
            ok=error is None,
            action=action,
            target="sync",
            state_version=self.store.state.version,
            error=(
                ErrorEnvelope(
                    code=error.code,
                    message=error.message,
                    target=error.target or "sync",
                    correlation_id=cid,
                )
                if error
                else None
            ),
            applied=list(applied or []),
            session_id=session_id,
        )
        log.info(
            "command",
            extra={
                "extra": {
                    "action": action,
                    "target": "sync",
                    "ok": ack.ok,
                    **({"code": error.code} if error else {}),
                }
            },
        )
        self.router._broadcast(ack)  # noqa: SLF001 - same package; one ack per call
        return ack
