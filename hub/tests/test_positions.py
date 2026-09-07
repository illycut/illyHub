from __future__ import annotations

import random

from illyhub_hub.positions import PositionTracker


class Device:
    """A player whose counter ticks at ``anchor + k`` seconds; reports floor seconds."""

    def __init__(self, anchor: float) -> None:
        self.anchor = anchor

    def true_ms(self, now: float) -> float:
        return (now - self.anchor) * 1000

    def report_ms(self, now: float) -> int:
        return int((now - self.anchor) // 1) * 1000


def test_fixed_phase_polling_alone_cannot_narrow_the_interval() -> None:
    dev, t = Device(anchor=10.37), PositionTracker()
    t.set_playing(True, 11.0)
    for k in range(5):
        now = 11.0 + k  # exactly 1 Hz, fixed phase
        t.observe(dev.report_ms(now), now)
    assert t.width_s > 0.9  # the estimator is honest about that


def test_bisection_schedule_converges_under_150ms_over_five_minutes() -> None:
    rng = random.Random(7)
    worst = 0.0
    for _ in range(25):
        dev = Device(anchor=rng.uniform(0, 1) + 100)
        t = PositionTracker()
        now = 101.0 + rng.uniform(0, 1)
        t.set_playing(True, now)
        est = t.observe(dev.report_ms(now), now)
        polls = 0
        while now < 101.0 + 300:
            now += t.next_poll_in(now, 1.0) + rng.uniform(-0.01, 0.01)  # scheduling jitter
            est = t.observe(dev.report_ms(now), now)
            polls += 1
            if polls > 4:  # after the bisection settles, every estimate must be tight
                worst = max(worst, abs(est.position_ms - dev.true_ms(now)))
        assert 0.5 <= polls / 300 <= 1.5  # roughly 1 Hz
        assert est.confidence == 0.9
    assert worst < 150, worst


def test_edge_observations_are_tight_immediately() -> None:
    dev, t = Device(anchor=5.0), PositionTracker(edge_slack_s=0.2)
    t.set_playing(True, 5.0)
    est = t.observe(dev.report_ms(7.02), 7.02, is_edge=True)  # event 20 ms after the tick
    assert abs(est.position_ms - dev.true_ms(7.02)) < 120
    est = t.observe(dev.report_ms(8.03), 8.03, is_edge=True)
    assert est.confidence == 0.9


def test_seek_and_track_change_reset_and_pause_freezes() -> None:
    dev, t = Device(anchor=0.0), PositionTracker()
    t.set_playing(True, 0.5)
    for now in (0.5, 1.7, 2.4):
        t.observe(dev.report_ms(now), now)
    # A contradictory report (user scrubbed to 60s) restarts the interval instead of merging.
    est = t.observe(60_000, 3.0)
    assert 59_000 <= est.position_ms <= 61_000 and t.width_s == 1.0
    assert t.observations == 1 and est.confidence == 0.5  # the jump is the only evidence
    # Pause freezes the last estimate; resume forgets it.
    t.set_playing(False, 4.0)
    frozen = t.estimate(9.0)
    assert frozen is not None and frozen.confidence == 1.0 and frozen.position_ms == t.frozen_ms
    assert t.estimate(9.0) == frozen
    t.set_playing(True, 10.0)
    assert t.estimate(10.0) is None and t.next_poll_in(10.0) == 1.0
    t.reset()
    assert t.width_s == 1.0 and t.observations == 0


def test_observe_while_paused_reports_the_raw_value() -> None:
    t = PositionTracker()
    est = t.observe(12_000, 100.0)
    assert est.position_ms == 12_000 and t.to_position(100.0) is not None
    assert t.to_position(150.0).position_ms == 12_000  # not playing: no interpolation
