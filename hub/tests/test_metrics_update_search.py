"""Phase 8 units: metrics (percentiles, rollover, persistence, summary), update manager (check,
apply, status parsing, disabled/running), search fan-out (deadline, partial, min length)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from illyhub_hub.content import BrowseItem, ContentRef, NeedsLinkError
from illyhub_hub.metrics import RESERVOIR, Metrics, _p, vendors_of
from illyhub_hub.services.search import SearchService, _round_robin
from illyhub_hub.update import UpdateManager, remote_allowed

# --------------------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------------------


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def test_percentiles_and_vendors_of() -> None:
    assert _p([], 0.5) is None
    assert _p([10.0], 0.95) == 10.0
    assert _p([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5) == 5
    assert _p([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10.0
    assert vendors_of(["heos:heos-1", "heos-2", "sonos:sonos-g1", "denon-x:main"]) == [
        "heos",
        "sonos",
        "denon",
    ]
    assert vendors_of(["all", "sync"]) == []


def test_metrics_day_rollover_persists_and_loads(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 8, 23, 59, 0, tzinfo=UTC))
    m = Metrics(tmp_path / "m.sqlite", clock=clock)
    m.record_command("play", True, 120.0, ["heos"])
    m.record_command("play", False, 300.0, ["heos"])
    m.record_command("volume", True, 20.0, ["sonos"])
    m.record_reconnect("sonos")
    m.set_ws_clients(3)
    m.set_ws_clients(1)
    m.tick_uptime(60)
    today = m.today()
    assert today.commands == 3 and today.commands_failed == 1
    assert today.latency_by_action["play"].p95_ms == 300.0
    assert today.latency_by_vendor["sonos"].count == 1
    assert today.reconnects == {"sonos": 1} and today.ws_clients_peak == 3
    assert today.uptime_s == 60.0
    # cross midnight: yesterday is persisted and today starts empty
    clock.now += timedelta(minutes=2)
    m.tick_uptime(60)
    assert m.today().day == "2026-09-09" and m.today().commands == 0
    assert m.drain_rollup() is True  # the finished day is written off-loop by the uptime tick
    days = m._load_days()  # noqa: SLF001 - unit test
    assert len(days) == 1 and days[0].day == "2026-09-08" and days[0].commands == 3
    assert days[0].uptime_pct == round(60 / 864.0, 2)
    # retention: an old day is trimmed on the next persist
    m._persist(days[0].model_copy(update={"day": "2020-01-01"}))  # noqa: SLF001
    m.flush_today()
    assert all(d.day != "2020-01-01" for d in m._load_days())  # noqa: SLF001


def test_metrics_sync_sessions_and_summary(tmp_path: Path) -> None:
    m = Metrics(tmp_path / "m.sqlite")
    m.record_sync_session(
        start_delta_ms=150, drift_mean_ms=80.0, drift_p95_ms=210.0, corrections=2, lost=False
    )
    m.record_sync_session(
        start_delta_ms=-90, drift_mean_ms=None, drift_p95_ms=None, corrections=0, lost=True
    )
    s = m.today().sync
    assert s.sessions == 2 and s.corrections == 2 and s.lost == 1
    assert s.start_delta_p95_ms == 150.0 and s.drift_mean_ms == 80.0
    m.record_command("play", True, 100.0, ["heos"])
    m.record_command("volume", True, 40.0, ["sonos"])
    m.record_reconnect("heos")
    # household-facing: no percentages, no p95, no client counts (those stay in /api/metrics)
    assert m.summary() == "Running. 2 commands, all worked."
    m.started_at = m._clock() - timedelta(minutes=5)  # noqa: SLF001
    text = m.summary()
    assert text.startswith("Running since ") and text.endswith(" today. 2 commands, all worked.")
    assert "%" not in text and "p95" not in text and "client" not in text
    m.record_command("play", False, 1.0, ["heos"])
    assert m.summary().endswith(" 3 commands, 1 didn't.")
    m.started_at = m._clock() - timedelta(days=1)
    assert "Running since yesterday at " in m.summary()
    m.started_at = m._clock() - timedelta(days=3)
    assert " at " in m.summary() and "yesterday" not in m.summary()
    assert Metrics(m.path).summary() == "Running. No commands yet today."
    assert m.maybe_log_summary() is True and m.maybe_log_summary() is False


def test_metrics_reservoir_and_action_whitelist(tmp_path: Path) -> None:
    m = Metrics(tmp_path / "m.sqlite")
    for i in range(RESERVOIR + 500):
        m.record_command("play", True, float(i), ["heos"])
    assert len(m._lat_action["play"]) == RESERVOIR  # noqa: SLF001
    assert m.today().latency_by_action["play"].count == RESERVOIR
    m.record_command("definitely_not_a_verb", True, 1.0, ["mars"])
    t = m.today()
    assert (
        t.commands_by_action["other"] == 1 and "definitely_not_a_verb" not in t.commands_by_action
    )
    assert "mars" not in t.latency_by_vendor


def test_metrics_rollup_is_deferred_and_today_seeds_from_store(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 8, 23, 59, 0, tzinfo=UTC))
    m = Metrics(tmp_path / "m.sqlite", clock=clock)
    m.record_command("play", True, 10.0, ["heos"])
    m.tick_uptime(30)
    clock.now += timedelta(minutes=2)
    m.tick_uptime(30)  # crosses midnight: the finished day is pending, not yet written
    assert m._pending_rollup is not None and m._load_days() == []  # noqa: SLF001
    assert m.drain_rollup() is True and m.drain_rollup() is False
    assert [d.day for d in m._load_days()] == ["2026-09-08"]  # noqa: SLF001
    # a restart mid-day continues today's counters from the flushed row
    m.record_command("volume", True, 5.0, ["sonos"])
    m.flush_today()
    m2 = Metrics(tmp_path / "m.sqlite", clock=clock)
    assert m2.today().commands == 1 and m2.today().uptime_s == 30.0
    assert [d.day for d in m2._load_days()] == ["2026-09-08"]  # today excluded  # noqa: SLF001


async def test_metrics_report_with_unreadable_db(tmp_path: Path) -> None:
    path = tmp_path / "m.sqlite"
    path.write_text("not a database")
    m = Metrics(path)
    report = await m.report()
    assert report.days == [] and m.last_error is not None


# --------------------------------------------------------------------------------------
# update manager
# --------------------------------------------------------------------------------------


class Git:
    def __init__(
        self,
        ahead: int = 0,
        fail: str | None = None,
        remote: str = "https://github.com/illycut/illyHub.git",
        branch: str = "main",
        upstream_exists: bool = True,
    ) -> None:
        self.ahead, self.fail, self.remote = ahead, fail, remote
        self.branch, self.upstream_exists = branch, upstream_exists
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str], cwd: Path, timeout_s: float) -> tuple[int, str, str]:
        self.calls.append(argv)
        sub = argv[1]
        if self.fail == sub:
            return 1, "", "boom"
        if sub == "remote":
            return 0, self.remote, ""
        if sub == "rev-parse":
            if "--abbrev-ref" in argv:
                return 0, self.branch, ""
            if "--verify" in argv:
                return (0, "def5678", "") if self.upstream_exists else (1, "", "")
            return 0, ("abc1234" if argv[-1] == "HEAD" else "def5678"), ""
        if sub == "fetch":
            return 0, "", ""
        if sub == "rev-list":
            return 0, str(self.ahead), ""
        if sub == "log":
            return 0, "\n".join(["one", "two"][: self.ahead]), ""
        return 1, "", "?"


async def launcher_ok(argv: list[str], cwd: Path, log_path: Path, env: dict[str, str]) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("started\n")
    return 1


def mgr(tmp_path: Path, git: Git, **kw: Any) -> UpdateManager:
    kw.setdefault("allow", True)
    return UpdateManager(
        tmp_path, tmp_path / "data", version="1", runner=git, launcher=launcher_ok, **kw
    )


async def test_update_check_up_to_date_and_ahead(tmp_path: Path) -> None:
    m = UpdateManager(tmp_path, tmp_path / "data", version="1", runner=Git(0), launcher=launcher_ok)
    c = await m.check()
    assert c.available is False and c.remote.ahead_by == 0 and c.remote.summary == []
    assert m.last_check() is c
    m2 = UpdateManager(
        tmp_path, tmp_path / "data", version="1", runner=Git(2), launcher=launcher_ok
    )
    c2 = await m2.check()
    assert c2.available and c2.remote.summary == ["one", "two"]


async def test_update_check_git_error_is_reported_not_raised(tmp_path: Path) -> None:
    m = UpdateManager(
        tmp_path,
        tmp_path / "data",
        version="1",
        runner=Git(1, fail="rev-list"),
        launcher=launcher_ok,
    )
    c = await m.check()
    # raw git text never reaches the screen: one sentence for the app, the detail in the log
    assert c.available is False and c.error == "Couldn't check for updates right now."


async def test_update_apply_status_lifecycle_and_reload(tmp_path: Path) -> None:
    m = mgr(tmp_path, Git(), pid_alive_fn=lambda pid: True)
    assert (await m.status()).state == "idle"
    job = await m.apply()
    assert job.pid == 1
    st = await m.status()
    assert st.state == "running" and st.job_id == job.job_id and st.log_tail == ["started"]
    with pytest.raises(RuntimeError):
        await m.apply()
    Path(job.status_path).write_text("succeeded updated a -> b\n")
    st = await m.status()
    assert st.state == "succeeded" and st.message == "updated a -> b" and st.finished_at
    # a fresh manager over the same data dir sees the last job
    m2 = mgr(tmp_path, Git(), pid_alive_fn=lambda pid: True)
    assert (await m2.status()).state == "succeeded" and (await m2.status()).job_id == job.job_id
    # an unknown state word is still "running" while the child lives; up_to_date is terminal
    Path(job.status_path).write_text("weird thing\n")
    assert (await m2.status()).state == "running"
    Path(job.status_path).write_text("up_to_date already at abc1234\n")
    assert (await m2.status()).state == "up_to_date"


async def test_update_stale_running_job_is_reported_failed(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
    m = mgr(tmp_path, Git(), clock=clock, pid_alive_fn=lambda pid: False)
    job = await m.apply()
    assert (await m.status()).state == "running"  # young: not yet stale
    clock.now += timedelta(minutes=16)
    st = await m.status()
    assert st.state == "failed" and "did not finish" in (st.message or "")
    # a reloaded manager after a restart sees the same stale job the same way
    m2 = mgr(tmp_path, Git(), clock=clock, pid_alive_fn=lambda pid: False)
    assert (await m2.status()).state == "failed" and (await m2.status()).job_id == job.job_id
    # ...and a new apply is allowed again
    job2 = await m2.apply()
    assert job2.job_id != job.job_id


async def test_update_apply_serialises_concurrent_calls(tmp_path: Path) -> None:
    m = mgr(tmp_path, Git(), pid_alive_fn=lambda pid: True)
    results = await asyncio.gather(m.apply(), m.apply(), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, RuntimeError)]
    assert len(ok) == 1 and len(refused) == 1


async def test_update_apply_disabled_and_remote_refused(tmp_path: Path) -> None:
    m = mgr(tmp_path, Git(), allow=False)
    with pytest.raises(PermissionError):
        await m.apply()
    m = mgr(tmp_path, Git(remote="file:///Users/x/illyHub"))
    with pytest.raises(PermissionError, match="not an https:// or ssh://"):
        await m.apply()
    assert remote_allowed("git@github.com:illycut/illyHub.git")
    assert remote_allowed("ssh://git@github.com/x.git") and not remote_allowed("/local/path")


async def test_update_launcher_env_never_kicks_launchd(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    async def launcher(argv: list[str], cwd: Path, log_path: Path, env: dict[str, str]) -> int:
        seen.update(env)
        return 7

    m = UpdateManager(
        tmp_path, tmp_path / "data", version="1", allow=True, runner=Git(), launcher=launcher
    )
    await m.apply()
    assert seen["ILLYHUB_UPDATE_RESTART"] == "0" and seen["HUB_UPDATE_BRANCH"] == "main"


async def test_update_prunes_old_job_logs(tmp_path: Path) -> None:
    m = mgr(tmp_path, Git(), pid_alive_fn=lambda pid: True)
    for i in range(13):
        job = await m.apply()
        Path(job.status_path).write_text("failed test\n")
        Path(job.log_path).touch()
        _ = i
    assert len(list((tmp_path / "data" / "update").glob("*.log"))) <= 10


# --------------------------------------------------------------------------------------
# search fan-out
# --------------------------------------------------------------------------------------


def item(service: str, title: str) -> BrowseItem:
    return BrowseItem(
        content_ref=ContentRef(service=service, kind="album", id=title.lower()),
        title=title,  # type: ignore[arg-type]
    )


async def test_search_min_length_limit_and_grouping() -> None:
    async def tidal(q: str, limit: int) -> dict[str, list[BrowseItem]]:
        return {"albums": [item("tidal", f"T{i}") for i in range(10)], "tracks": []}

    svc = SearchService([("tidal", lambda: True, tidal)])
    with pytest.raises(ValueError):
        await svc.search(" a ")
    r = await svc.search("  ab  cd ", limit=3)
    assert r.query == "ab cd" and len(r.albums) == 3 and r.services == ["tidal"]
    assert r.partial is False and r.errors == {}


async def test_search_deadline_partial_and_needs_link() -> None:
    async def slow(q: str, limit: int) -> dict[str, Any]:
        await asyncio.sleep(0.5)
        return {"albums": [item("tidal", "late")]}

    async def fast(q: str, limit: int) -> dict[str, Any]:
        return {"playlists": [item("ytmusic", "quick")]}

    async def broken(q: str, limit: int) -> dict[str, Any]:
        raise RuntimeError("nope")

    async def unlinked(q: str, limit: int) -> dict[str, Any]:
        raise NeedsLinkError("pandora", "Pandora is set up in the HEOS app.")

    svc = SearchService(
        [
            ("tidal", lambda: True, slow),
            ("ytmusic", lambda: True, fast),
            ("spotify", lambda: True, broken),
            ("pandora", lambda: True, unlinked),
            ("amazon", lambda: False, fast),
        ],
        deadline_s=0.05,
    )
    r = await svc.search("quick")
    assert r.partial is True and r.services == ["ytmusic"]
    assert r.errors["tidal"] == "Tidal didn't answer in time."
    assert r.errors["spotify"] == "Spotify search failed."
    assert r.errors["pandora"] == "Pandora is set up in the HEOS app."
    assert r.errors["amazon"] == "Amazon is not connected."
    assert [p.title for p in r.playlists] == ["quick"]


def test_round_robin_interleaves_services() -> None:
    a = [item("tidal", f"T{i}") for i in range(3)]
    b = [item("ytmusic", f"Y{i}") for i in range(1)]
    assert [x.title for x in _round_robin([a, b])] == ["T0", "Y0", "T1", "T2"]
    assert _round_robin([]) == []


async def test_search_round_robin_keeps_a_small_service_visible() -> None:
    async def big(q: str, limit: int) -> dict[str, Any]:
        return {"albums": [item("tidal", f"T{i}") for i in range(40)]}

    async def small(q: str, limit: int) -> dict[str, Any]:
        return {"albums": [item("ytmusic", "Y0")]}

    svc = SearchService([("tidal", lambda: True, big), ("ytmusic", lambda: True, small)])
    r = await svc.search("xx", limit=4)
    assert [x.title for x in r.albums] == ["T0", "Y0", "T1", "T2"]


async def test_search_stations_substring() -> None:
    async def stations() -> list[BrowseItem]:
        return [
            BrowseItem(
                content_ref=ContentRef(service="pandora", kind="station", id="chill"),
                title="Chill Radio",
            ),
            BrowseItem(
                content_ref=ContentRef(service="pandora", kind="station", id="rock"),
                title="Rock Block",
            ),
        ]

    svc = SearchService([], stations=stations)
    r = await svc.search("chill")
    assert [s.title for s in r.stations] == ["Chill Radio"] and r.services == ["pandora"]
