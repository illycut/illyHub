"""Sub-second playback position estimation from whole-second device reports.

Sonos ``GetPositionInfo`` and HEOS ``player_now_playing_progress`` both report position at
roughly one-second resolution. Reading such a value at local time ``now`` only tells us the
device's counter incremented somewhere in the previous second. Modelling the device as a
counter that ticks at ``anchor + k`` seconds, every observation ``(now, reported)`` bounds the
anchor to the half-open interval ``(now - reported - resolution, now - reported]``. Intersecting
successive observations narrows the anchor; the position at any time is ``now - anchor``.

For a poller this only helps if the poll *phase* varies, so :meth:`PositionTracker.next_poll_in`
schedules each poll at the midpoint of the current interval (a bisection): three polls take the
uncertainty from one second to 125 ms. Event-driven sources (HEOS) send the report right after
the tick, so :meth:`observe` with ``is_edge=True`` uses a narrow ``edge_slack_s`` window.

A report that contradicts the current interval (seek, track change, resume after pause) resets
the estimate; a pause freezes it. Everything here is pure and clock-injected for testing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .state import Position, now

HIGH_CONFIDENCE_WIDTH_S = 0.25
MAX_INTERVAL_S = 1.0


@dataclass
class Estimate:
    position_ms: int
    confidence: float
    width_s: float


class PositionTracker:
    """Anchor-interval estimator for one side. All times are seconds on a monotonic clock."""

    def __init__(self, *, edge_slack_s: float = 0.2) -> None:
        self.edge_slack_s = edge_slack_s
        self.lo: float | None = None  # anchor interval (lo, hi]
        self.hi: float | None = None
        self.playing = False
        self.frozen_ms: int | None = None
        self.observations = 0

    # -- input ----------------------------------------------------------------------

    def reset(self) -> None:
        self.lo = self.hi = None
        self.frozen_ms = None
        self.observations = 0

    def set_playing(self, playing: bool, now_s: float) -> None:
        """Pause freezes the current estimate; resuming starts a fresh anchor search."""
        if playing == self.playing:
            return
        if not playing:
            est = self.estimate(now_s)
            self.frozen_ms = est.position_ms if est else None
            self.lo = self.hi = None
        else:
            self.frozen_ms = None
            self.lo = self.hi = None
            self.observations = 0
        self.playing = playing

    def observe(
        self, reported_ms: int, now_s: float, *, resolution_s: float = 1.0, is_edge: bool = False
    ) -> Estimate:
        """Fold one device report into the estimate and return the position at ``now_s``."""
        self.observations += 1
        reported_s = reported_ms / 1000.0
        hi = now_s - reported_s
        width = self.edge_slack_s if is_edge else resolution_s
        lo = hi - width
        if self.lo is None or self.hi is None or lo >= self.hi or hi <= self.lo:
            # First report, or a contradiction (seek / track change): start over. This report is
            # the only evidence we have, so the observation count restarts at one.
            self.lo, self.hi = lo, hi
            self.observations = 1
        else:
            self.lo, self.hi = max(self.lo, lo), min(self.hi, hi)
        self.frozen_ms = None
        if not self.playing:
            self.frozen_ms = reported_ms
        return self.estimate(now_s) or Estimate(reported_ms, 0.5, width)

    # -- output ---------------------------------------------------------------------

    @property
    def width_s(self) -> float:
        if self.lo is None or self.hi is None:
            return MAX_INTERVAL_S
        return self.hi - self.lo

    def estimate(self, now_s: float) -> Estimate | None:
        if self.frozen_ms is not None and not self.playing:
            return Estimate(self.frozen_ms, 1.0, 0.0)
        if self.lo is None or self.hi is None:
            return None
        anchor = (self.lo + self.hi) / 2
        pos = max(0.0, now_s - anchor)
        width = self.width_s
        confidence = 0.9 if width <= HIGH_CONFIDENCE_WIDTH_S and self.observations >= 2 else 0.5
        return Estimate(int(round(pos * 1000)), confidence, width)

    def next_poll_in(self, now_s: float, base_interval_s: float = 1.0) -> float:
        """Seconds until the next poll: ~``base_interval_s`` apart, phased onto the anchor midpoint.

        A poll landing exactly at ``midpoint + k`` halves the interval regardless of which side
        the true anchor is on. Once the interval is already tight, the schedule just holds phase.
        """
        if self.lo is None or self.hi is None:
            return base_interval_s
        mid = (self.lo + self.hi) / 2
        target = mid + math.floor(now_s - mid) + 1  # first midpoint-phased instant after now
        while target - now_s < base_interval_s / 2:
            target += 1.0
        return target - now_s

    def to_position(self, now_s: float) -> Position | None:
        est = self.estimate(now_s)
        if est is None:
            return None
        return Position(position_ms=est.position_ms, reported_at=now(), confidence=est.confidence)
