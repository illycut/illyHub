"""Observability for the PRD §10 success metrics (ai-dev #73).

In-memory counters and timers for today, rolled up once a day into ``metrics.sqlite`` under
``HUB_DATA_DIR`` (30-day retention). Everything is fed by hooks that already exist: router acks
(command latency, commands per day), the state store (adapter reconnects, uptime ticks from the
health check), the WebSocket layer (client count) and the sync engine (start delta, drift per
session). ``GET /api/metrics`` returns today's live numbers plus the rollups; ``/summary`` is
one paragraph of plain text that is also logged once a day. No new dependencies.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from .logsetup import get_logger

log = get_logger("metrics")

RETENTION_DAYS = 30
UPTIME_TICK_S = 60.0  # the health loop ticks uptime once a minute
VENDORS = ("heos", "sonos", "denon")
RESERVOIR = 2000  # latency samples kept per key (oldest dropped), bounding memory per day
# Only the hub's own command verbs are keyed; anything else (a future action name, a typo in a
# caller) is counted under "other" so the table cannot grow from unexpected input.
KNOWN_ACTIONS: frozenset[str] = frozenset(
    {
        "play",
        "pause",
        "toggle",
        "stop",
        "next",
        "prev",
        "seek",
        "skip",
        "volume",
        "linked_volume",
        "mute",
        "zone_power",
        "group",
        "ungroup",
        "play_content",
        "play_station",
        "sync_play",
        "sync_stop",
        "sync_retry",
        "queue_jump",
        "play_mode",
        "airplay_outputs",
        "pandora_sync_start",
        "pandora_sync_stop",
    }
)


def _push(samples: list[float], value: float) -> None:
    samples.append(value)
    if len(samples) > RESERVOIR:
        del samples[: len(samples) - RESERVOIR]


def _p(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(q * (len(s) - 1))))
    return round(s[k], 1)


class LatencyStats(BaseModel):
    count: int = 0
    p50_ms: float | None = None
    p95_ms: float | None = None


class SyncStats(BaseModel):
    sessions: int = 0
    start_delta_p50_ms: float | None = None
    start_delta_p95_ms: float | None = None
    drift_mean_ms: float | None = None
    drift_p95_ms: float | None = None
    corrections: int = 0
    lost: int = 0


class DayMetrics(BaseModel):
    day: str
    uptime_s: float = 0.0
    uptime_pct: float | None = None  # of the elapsed part of the day (today) or the full day
    commands: int = 0
    commands_failed: int = 0
    commands_by_action: dict[str, int] = Field(default_factory=dict)
    latency_by_action: dict[str, LatencyStats] = Field(default_factory=dict)
    latency_by_vendor: dict[str, LatencyStats] = Field(default_factory=dict)
    reconnects: dict[str, int] = Field(default_factory=dict)
    ws_clients_peak: int = 0
    sync: SyncStats = Field(default_factory=SyncStats)


class MetricsResponse(BaseModel):
    today: DayMetrics
    days: list[DayMetrics]  # newest first, up to RETENTION_DAYS, today excluded
    ws_clients: int
    generated_at: datetime


class Metrics:
    """Live counters for the current UTC day plus a SQLite store of finished days."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.path = path
        self._clock = clock or (lambda: datetime.now(UTC))
        self.ws_clients = 0
        self._day: date = self._clock().date()
        self._reset_day()
        self.last_error: str | None = None
        self._last_summary_day: date | None = None
        self._pending_rollup: DayMetrics | None = None  # finished day awaiting an off-loop write
        self.started_at: datetime | None = None  # UTC; set by the runtime once the hub is up
        self._seed_from_store()

    def _seed_from_store(self) -> None:
        """A restart mid-day continues that day's counters from the row ``flush_today`` wrote."""
        try:
            stored = self._load_day(self._day.isoformat())
        except sqlite3.Error as exc:
            self.last_error = str(exc)
            return
        if stored is None:
            return
        self._uptime_s = stored.uptime_s
        self._commands = stored.commands
        self._failed = stored.commands_failed
        self._by_action.update(stored.commands_by_action)
        self._reconnects.update(stored.reconnects)
        self._ws_peak = stored.ws_clients_peak
        self._sync_sessions = stored.sync.sessions
        self._sync_corrections = stored.sync.corrections
        self._sync_lost = stored.sync.lost
        # Latency samples are not stored per value; the percentiles restart from the stored
        # aggregate count only (they refill quickly at 1 Hz).

    # -- day buffer ----------------------------------------------------------------------

    def _reset_day(self) -> None:
        self._uptime_s = 0.0
        self._commands = 0
        self._failed = 0
        self._by_action: dict[str, int] = defaultdict(int)
        self._lat_action: dict[str, list[float]] = defaultdict(list)
        self._lat_vendor: dict[str, list[float]] = defaultdict(list)
        self._reconnects: dict[str, int] = defaultdict(int)
        self._ws_peak = 0
        self._sync_starts: list[float] = []
        self._sync_drift_mean: list[float] = []
        self._sync_drift_p95: list[float] = []
        self._sync_sessions = 0
        self._sync_corrections = 0
        self._sync_lost = 0
        self._day_started_at = self._clock()

    def _roll_if_needed(self) -> None:
        """Close the day buffer at UTC midnight. The SQLite write is deferred to
        :meth:`drain_rollup` (called off-loop from the uptime tick) so hot paths never block."""
        today = self._clock().date()
        if today != self._day:
            finished = self.today()
            finished.uptime_pct = round(min(100.0, finished.uptime_s / 864.0), 2)
            self._pending_rollup = finished
            self._day = today
            self._reset_day()

    def drain_rollup(self) -> bool:
        """Write a pending finished day (sync; call via ``asyncio.to_thread``)."""
        pending, self._pending_rollup = self._pending_rollup, None
        if pending is None:
            return False
        try:
            self._persist(pending)
        except sqlite3.Error as exc:
            self.last_error = str(exc)
            log.warning("metrics rollup failed", extra={"extra": {"error": str(exc)}})
            return False
        return True

    # -- hooks -----------------------------------------------------------------------------

    def tick_uptime(self, seconds: float = UPTIME_TICK_S) -> None:
        self._roll_if_needed()
        self._uptime_s += seconds

    def record_command(
        self, action: str, ok: bool, latency_ms: float, vendors: list[str] | None = None
    ) -> None:
        self._roll_if_needed()
        key = action if action in KNOWN_ACTIONS else "other"
        self._commands += 1
        if not ok:
            self._failed += 1
        self._by_action[key] += 1
        _push(self._lat_action[key], latency_ms)
        for v in vendors or []:
            if v in VENDORS:
                _push(self._lat_vendor[v], latency_ms)

    def record_reconnect(self, adapter: str) -> None:
        self._roll_if_needed()
        self._reconnects[adapter] += 1

    def set_ws_clients(self, n: int) -> None:
        self._roll_if_needed()
        self.ws_clients = max(0, n)
        self._ws_peak = max(self._ws_peak, self.ws_clients)

    def record_sync_session(
        self,
        *,
        start_delta_ms: int | None,
        drift_mean_ms: float | None,
        drift_p95_ms: float | None,
        corrections: int,
        lost: bool,
    ) -> None:
        self._roll_if_needed()
        self._sync_sessions += 1
        if start_delta_ms is not None:
            self._sync_starts.append(abs(start_delta_ms))
        if drift_mean_ms is not None:
            self._sync_drift_mean.append(abs(drift_mean_ms))
        if drift_p95_ms is not None:
            self._sync_drift_p95.append(abs(drift_p95_ms))
        self._sync_corrections += corrections
        if lost:
            self._sync_lost += 1

    # -- read ------------------------------------------------------------------------------

    def today(self) -> DayMetrics:
        elapsed = max(1.0, (self._clock() - self._day_started_at).total_seconds())
        return DayMetrics(
            day=self._day.isoformat(),
            uptime_s=round(self._uptime_s, 1),
            uptime_pct=round(min(100.0, 100.0 * self._uptime_s / elapsed), 2),
            commands=self._commands,
            commands_failed=self._failed,
            commands_by_action=dict(self._by_action),
            latency_by_action={
                a: LatencyStats(count=len(v), p50_ms=_p(v, 0.5), p95_ms=_p(v, 0.95))
                for a, v in self._lat_action.items()
            },
            latency_by_vendor={
                a: LatencyStats(count=len(v), p50_ms=_p(v, 0.5), p95_ms=_p(v, 0.95))
                for a, v in self._lat_vendor.items()
            },
            reconnects=dict(self._reconnects),
            ws_clients_peak=self._ws_peak,
            sync=SyncStats(
                sessions=self._sync_sessions,
                start_delta_p50_ms=_p(self._sync_starts, 0.5),
                start_delta_p95_ms=_p(self._sync_starts, 0.95),
                drift_mean_ms=(
                    round(statistics.fmean(self._sync_drift_mean), 1)
                    if self._sync_drift_mean
                    else None
                ),
                drift_p95_ms=_p(self._sync_drift_p95, 0.95),
                corrections=self._sync_corrections,
                lost=self._sync_lost,
            ),
        )

    async def report(self) -> MetricsResponse:
        self._roll_if_needed()
        await asyncio.to_thread(self.drain_rollup)
        days = await asyncio.to_thread(self._load_days)
        return MetricsResponse(
            today=self.today(), days=days, ws_clients=self.ws_clients, generated_at=self._clock()
        )

    def summary(self) -> str:
        """Two household-facing sentences, e.g. "Running since 7:37 am today. 10 commands, all
        worked." Percentages, latencies and client counts stay in ``GET /api/metrics``."""
        t = self.today()
        if self.started_at is None:
            since = "Running."
        else:
            start = self.started_at.astimezone()  # hub Mac local time
            clock = f"{start.hour % 12 or 12}:{start.minute:02d} {start:%p}".lower()
            days_ago = (self._clock().astimezone().date() - start.date()).days
            if days_ago <= 0:
                since = f"Running since {clock} today."
            elif days_ago == 1:
                since = f"Running since yesterday at {clock}."
            else:
                since = f"Running since {start:%b} {start.day} at {clock}."
        plural = "s" if t.commands != 1 else ""
        if t.commands == 0:
            commands = "No commands yet today."
        elif t.commands_failed == 0:
            commands = f"{t.commands} command{plural}, all worked."
        else:
            commands = f"{t.commands} command{plural}, {t.commands_failed} didn't."
        return f"{since} {commands}"

    def maybe_log_summary(self) -> bool:
        """Log the summary once per day (called from the uptime tick)."""
        today = self._clock().date()
        if self._last_summary_day == today:
            return False
        self._last_summary_day = today
        log.info("daily metrics", extra={"extra": {"summary": self.summary()}})
        return True

    # -- sqlite ----------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        return conn

    def _persist(self, day: DayMetrics) -> None:
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO days (day, payload) VALUES (?, ?)",
                (day.day, day.model_dump_json()),
            )
            cutoff = (date.fromisoformat(day.day) - timedelta(days=RETENTION_DAYS)).isoformat()
            conn.execute("DELETE FROM days WHERE day < ?", (cutoff,))

    def _load_day(self, day: str) -> DayMetrics | None:
        if not self.path.exists():
            return None
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute("SELECT payload FROM days WHERE day = ?", (day,)).fetchone()
        if row is None:
            return None
        try:
            return DayMetrics.model_validate(json.loads(row[0]))
        except ValueError:
            return None

    def _load_days(self) -> list[DayMetrics]:
        """Finished days, newest first; today is served live and excluded here."""
        if not self.path.exists():
            return []
        try:
            with contextlib.closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT payload FROM days WHERE day < ? ORDER BY day DESC LIMIT ?",
                    (self._day.isoformat(), RETENTION_DAYS),
                ).fetchall()
        except sqlite3.Error as exc:
            self.last_error = str(exc)
            return []
        out: list[DayMetrics] = []
        for (payload,) in rows:
            try:
                out.append(DayMetrics.model_validate(json.loads(payload)))
            except ValueError:
                continue
        return out

    def flush_today(self) -> None:
        """Persist the live day (on shutdown), so a restart mid-day does not lose it. The
        rollup on the next day boundary replaces it."""
        try:
            self._persist(self.today())
        except sqlite3.Error as exc:
            self.last_error = str(exc)


def vendors_of(applied: list[str]) -> list[str]:
    """Vendor names from applied target ids (``heos:…`` / ``heos-…`` / ``sonos…`` /
    ``denon-…``), deduplicated in order."""
    out: list[str] = []
    for t in applied:
        for v in VENDORS:
            if t.startswith(v) and v not in out:
                out.append(v)
    return out
