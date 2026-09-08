"""Phase 8 API: queue read/jump, play mode (and its Sync Play lock), search fan-out, metrics,
self-update (fake git runner + fake launcher), dev scenarios, header policy."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from illyhub_hub.adapters.fake import HEOS_PLAYER, KITCHEN
from illyhub_hub.api import HubRuntime, create_app
from illyhub_hub.config import Settings
from illyhub_hub.messages import PLAY_MODE_LOCKED, UPDATE_DISABLED, UPDATE_RUNNING
from illyhub_hub.update import UpdateManager

HDR = {"X-Illyhub": "1"}
HEOS_SIDE = "heos:heos-1"
ALBUM = {"service": "tidal", "kind": "album", "id": "101"}


def fake_settings(tmp_path: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = dict(
        fake_devices=True,
        fake_tidal=True,
        fake_ytmusic=True,
        fake_pandora=True,
        position_poll_s=0.02,
        command_coalesce_s=0.0,
        metrics_tick_s=0.05,
        sync_monitor_s=0.02,
        sync_correction_gap_s=0.3,
        sync_track_grace_s=0.15,
        sync_lookahead_ms=0,
        sync_drift_ms=100,
        data_dir=str(tmp_path / "data"),
        app_dir=str(tmp_path / "no-app"),
        fonts_dir=str(tmp_path / "no-fonts"),
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


class FakeGit:
    """Scripted ``git`` answers keyed on the first argument after ``git``."""

    def __init__(self, ahead: int = 2, fail: str | None = None) -> None:
        self.ahead = ahead
        self.fail = fail
        self.calls: list[list[str]] = []

    remote = "https://github.com/illycut/illyHub.git"

    async def __call__(self, argv: list[str], cwd: Path, timeout_s: float) -> tuple[int, str, str]:
        self.calls.append(argv)
        sub = argv[1]
        if self.fail == sub:
            return 128, "", f"fatal: {sub} failed"
        if sub == "remote":
            return 0, self.remote + "\n", ""
        if sub == "rev-parse":
            if "--abbrev-ref" in argv:
                return 0, "main\n", ""
            if "--verify" in argv:
                return 0, "def5678\n", ""
            return 0, ("abc1234\n" if argv[-1] == "HEAD" else "def5678\n"), ""
        if sub == "fetch":
            return 0, "", ""
        if sub == "rev-list":
            return 0, f"{self.ahead}\n", ""
        if sub == "log":
            return 0, "\n".join(f"commit {i}" for i in range(self.ahead)) + "\n", ""
        return 1, "", "unknown"


class FakeLauncher:
    """Pretends to start ops/update.sh: records the call and lets tests write the status file."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, argv: list[str], cwd: Path, log_path: Path, env: dict[str, str]
    ) -> int:
        self.calls.append({"argv": argv, "cwd": cwd, "log": log_path, "env": env})
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("[job] update started\n")
        return 4242


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, HubRuntime]]:
    app = create_app(fake_settings(tmp_path))
    rt: HubRuntime = app.state.runtime
    git, launcher = FakeGit(), FakeLauncher()
    rt.update = UpdateManager(
        tmp_path / "repo",
        tmp_path / "data",
        version="0.1.0",
        allow=True,
        runner=git,
        launcher=launcher,
        pid_alive_fn=lambda pid: True,
    )
    rt.exit_fn = lambda code: exits.append(code)
    exits: list[int] = []
    rt.test_exits = exits  # type: ignore[attr-defined]
    rt.test_git = git  # type: ignore[attr-defined]
    rt.test_launcher = launcher  # type: ignore[attr-defined]
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c, rt


async def wait_for(pred, timeout: float = 3.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def play_album(c: httpx.AsyncClient, target: str = HEOS_PLAYER) -> None:
    r = await c.post("/api/play", json={"target": target, "content_ref": ALBUM})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------------------
# queue
# --------------------------------------------------------------------------------------


async def test_queue_read_publishes_state_and_flags_current(client) -> None:
    c, rt = client
    await play_album(c)
    r = await c.get(f"/api/queue/{HEOS_SIDE}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 9 and body["total"] == 9 and body["truncated"] is False
    assert body["current_index"] == 0 and body["source"] == "tidal"
    first = body["items"][0]
    assert first["title"] == "Signal 1" and first["content_ref"] == {
        "service": "tidal",
        "kind": "track",
        "id": "10101",
    }
    assert rt.store.state.queues[HEOS_SIDE].items[0].title == "Signal 1"


async def test_queue_unknown_side_and_empty_queue(client) -> None:
    c, _rt = client
    r = await c.get("/api/queue/room:nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_target"
    r = await c.get(f"/api/queue/{HEOS_SIDE}")
    assert r.status_code == 200 and r.json()["items"] == [] and r.json()["total"] == 0


async def test_queue_jump_moves_now_playing_and_streams_delta(client) -> None:
    c, rt = client
    await play_album(c)
    r = await c.post("/api/queue/jump", json={"target": HEOS_PLAYER, "index": 3})
    assert r.status_code == 200, r.text
    ack = r.json()
    assert ack["ok"] and ack["action"] == "queue_jump" and ack["applied"] == [HEOS_SIDE]
    assert rt.store.state.now_playing[HEOS_SIDE].title == "Harmonic 4"
    assert rt.store.state.queues[HEOS_SIDE].current_index == 3


async def test_queue_jump_out_of_range_is_invalid_argument(client) -> None:
    c, _rt = client
    await play_album(c)
    await c.get(f"/api/queue/{HEOS_SIDE}")
    r = await c.post("/api/queue/jump", json={"target": HEOS_PLAYER, "index": 40})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_argument"
    r = await c.post("/api/queue/jump", json={"target": HEOS_PLAYER, "index": -1})
    assert r.status_code == 422


async def test_queue_truncation_and_vendor_app_change_scenario(client) -> None:
    c, rt = client
    rt.router.queue_max = 5
    await play_album(c)
    r = await c.get(f"/api/queue/{HEOS_SIDE}")
    body = r.json()
    assert len(body["items"]) == 5 and body["total"] == 9 and body["truncated"] is True
    rt.router.queue_max = 200
    r = await c.post("/api/dev/fake/queue_change")
    assert r.status_code == 200 and r.json()["result"]["queued"] == 10
    assert len(rt.store.state.queues[HEOS_SIDE].items) == 10
    assert rt.store.state.queues[HEOS_SIDE].items[-1].title == "Encore 10"


async def test_queue_delta_path_is_depth_two(client) -> None:
    c, rt = client
    seen: list[list[str]] = []
    rt.store.subscribe(lambda _s, changed: seen.append(changed))
    await play_album(c)
    assert any(f"queues.{HEOS_SIDE}" in ch for ch in seen)


# --------------------------------------------------------------------------------------
# play mode
# --------------------------------------------------------------------------------------


async def test_play_mode_sets_shuffle_and_repeat_on_the_side(client) -> None:
    c, rt = client
    r = await c.post("/api/playmode", json={"target": HEOS_PLAYER, "shuffle": True})
    assert r.status_code == 200, r.text
    assert rt.store.state.sides[HEOS_SIDE].play_mode.model_dump() == {
        "shuffle": True,
        "repeat": "off",
    }
    r = await c.post("/api/playmode", json={"target": HEOS_PLAYER, "repeat": "all"})
    assert r.status_code == 200
    assert rt.store.state.players[HEOS_PLAYER].play_mode.model_dump() == {
        "shuffle": True,
        "repeat": "all",
    }
    r = await c.post("/api/playmode", json={"target": KITCHEN, "repeat": "one", "shuffle": False})
    assert r.status_code == 200
    kitchen_side = rt.fakes.sonos.side_id()
    assert rt.store.state.sides[kitchen_side].play_mode.repeat == "one"


async def test_play_mode_body_validation(client) -> None:
    c, _rt = client
    r = await c.post("/api/playmode", json={"target": HEOS_PLAYER})
    assert r.status_code == 422
    r = await c.post("/api/playmode", json={"target": HEOS_PLAYER, "repeat": "twice"})
    assert r.status_code == 422


async def test_play_mode_scenario_from_vendor_app(client) -> None:
    c, rt = client
    r = await c.post("/api/dev/fake/play_mode_change")
    assert r.status_code == 200 and r.json()["result"]["play_mode"]["shuffle"] is True
    assert rt.store.state.sides[HEOS_SIDE].play_mode.shuffle is True


async def test_sync_play_forces_shuffle_off_and_locks_play_mode(client) -> None:
    c, rt = client
    await c.post("/api/playmode", json={"target": HEOS_PLAYER, "shuffle": True})
    await c.post("/api/playmode", json={"target": KITCHEN, "shuffle": True})
    body = {"content_ref": ALBUM, "heos_target": HEOS_PLAYER, "sonos_target": KITCHEN}
    r = await c.post("/api/sync/play", json=body)
    assert r.status_code == 200, r.text
    await wait_for(lambda: rt.store.state.sync.status == "locked")
    assert rt.store.state.sides[HEOS_SIDE].play_mode.shuffle is False
    assert rt.store.state.sides[rt.fakes.sonos.side_id()].play_mode.shuffle is False
    r = await c.post("/api/playmode", json={"target": HEOS_PLAYER, "shuffle": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "sync_active"
    assert r.json()["error"]["message"] == PLAY_MODE_LOCKED
    await c.post("/api/sync/stop")
    r = await c.post("/api/playmode", json={"target": HEOS_PLAYER, "shuffle": True})
    assert r.status_code == 200


# --------------------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------------------


async def test_search_groups_results_across_services_with_availability(client) -> None:
    c, _rt = client
    r = await c.get("/api/search", params={"q": "glow"})
    assert r.status_code == 200, r.text
    body = r.json()
    titles = [a["title"] for a in body["albums"]]
    assert "Warm Glow" in titles
    assert set(body["services"]) >= {"tidal", "ytmusic"} and body["errors"] == {}
    album = next(a for a in body["albums"] if a["title"] == "Warm Glow")
    assert (
        album["availability"]["heos"] is True and album["availability"]["reasons"]["heos"] is None
    )
    assert body["partial"] is False
    r = await c.get("/api/search", params={"q": "lantern"})
    yt = [a for a in r.json()["albums"] if a["content_ref"]["service"] == "ytmusic"]
    assert yt and yt[0]["availability"]["reasons"]["heos"] == "unsupported"


async def test_search_finds_stations_and_tracks(client) -> None:
    c, _rt = client
    r = await c.get("/api/search", params={"q": "chill"})
    body = r.json()
    assert any("Chill" in s["title"] for s in body["stations"])
    r = await c.get("/api/search", params={"q": "signal 1", "limit": 3})
    assert len(r.json()["tracks"]) <= 3 and r.json()["tracks"][0]["title"].startswith("Signal 1")


async def test_search_rejects_short_query_and_reports_unlinked(client) -> None:
    c, _rt = client
    r = await c.get("/api/search", params={"q": "a"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_argument"
    await c.post("/api/dev/fake/unlink_ytmusic")
    r = await c.get("/api/search", params={"q": "glow"})
    body = r.json()
    assert body["errors"]["ytmusic"] == "YouTube Music is not connected."
    assert "tidal" in body["services"] and "ytmusic" not in body["services"]


async def test_search_partial_on_slow_service(client, monkeypatch: pytest.MonkeyPatch) -> None:
    c, rt = client
    assert rt.search is not None
    rt.search.deadline_s = 0.05

    async def slow(_q: str, _l: int) -> dict[str, Any]:
        await asyncio.sleep(1.0)
        return {"albums": []}

    sources = list(rt.search._sources)  # noqa: SLF001 - test seam
    sources[1] = ("ytmusic", lambda: True, slow)
    rt.search._sources = sources  # noqa: SLF001
    r = await c.get("/api/search", params={"q": "glow"})
    body = r.json()
    assert body["partial"] is True
    assert body["errors"]["ytmusic"] == "YouTube Music didn't answer in time."
    assert "tidal" in body["services"]


# --------------------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------------------


async def test_metrics_count_commands_latency_and_ws_clients(client) -> None:
    c, rt = client
    assert rt.metrics is not None
    for _ in range(3):
        await c.post("/api/transport/play", json={"target": HEOS_PLAYER})
    await c.post("/api/volume", json={"target": KITCHEN, "level": 30})
    await c.post("/api/transport/play", json={"target": "room:nope"})
    r = await c.get("/api/metrics")
    assert r.status_code == 200, r.text
    today = r.json()["today"]
    assert today["commands"] == 5 and today["commands_failed"] == 1
    assert (
        today["commands_by_action"]["play"] == 4
        and today["latency_by_action"]["play"]["count"] == 4
    )
    assert today["latency_by_vendor"]["heos"]["count"] == 3
    assert today["latency_by_vendor"]["sonos"]["count"] == 1
    assert r.json()["ws_clients"] == 0 and r.json()["days"] == []
    await wait_for(lambda: rt.metrics.today().uptime_s > 0)  # type: ignore[union-attr]
    r = await c.get("/api/metrics/summary")
    text = r.json()["summary"]
    assert text.startswith("Running since ") and text.endswith(" today. 5 commands, 1 didn't.")


async def test_metrics_record_reconnects_and_sync_sessions(client) -> None:
    c, rt = client
    assert rt.metrics is not None
    await c.post("/api/dev/fake/disconnect_heos")
    await c.post("/api/dev/fake/reconnect_heos")
    assert rt.metrics.today().reconnects.get("heos") == 1
    body = {"content_ref": ALBUM, "heos_target": HEOS_PLAYER, "sonos_target": KITCHEN}
    await c.post("/api/sync/play", json=body)
    await wait_for(lambda: rt.store.state.sync.status == "locked")
    await c.post("/api/sync/stop")
    await wait_for(lambda: rt.metrics.today().sync.sessions == 1)  # type: ignore[union-attr]
    assert rt.metrics.today().sync.lost == 0


# --------------------------------------------------------------------------------------
# self-update
# --------------------------------------------------------------------------------------


async def test_update_check_reports_ahead_and_summary(client) -> None:
    c, rt = client
    r = await c.get("/api/hub/update/check")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True and body["remote"]["ahead_by"] == 2
    assert body["current"] == {"version": "0.1.0", "commit": "abc1234", "branch": "main"}
    assert body["remote"]["commit"] == "def5678" and body["remote"]["summary"] == [
        "commit 0",
        "commit 1",
    ]
    assert body["remote"]["tracking"] == "main"
    assert ["git", "fetch", "--quiet", "origin"] in rt.test_git.calls  # type: ignore[attr-defined]


async def test_update_check_surfaces_git_failure(client) -> None:
    c, rt = client
    rt.test_git.fail = "fetch"  # type: ignore[attr-defined]
    r = await c.get("/api/hub/update/check")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["error"] == "Couldn't check for updates right now."  # raw git text stays in the log


async def test_update_apply_requires_header_and_runs_detached(client) -> None:
    c, rt = client
    r = await c.post("/api/hub/update/apply")
    assert r.status_code == 403 and r.json()["code"] == "missing_header"
    r = await c.post("/api/hub/update/apply", headers=HDR)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    launched = rt.test_launcher.calls[0]  # type: ignore[attr-defined]
    assert launched["argv"][-1].endswith("ops/update.sh")
    assert launched["env"]["ILLYHUB_JOB_ID"] == job_id
    status = (await c.get("/api/hub/update/status")).json()
    assert status["state"] == "running" and status["job_id"] == job_id
    assert status["log_tail"] == ["[job] update started"]
    # a second apply while running is refused
    r = await c.post("/api/hub/update/apply", headers=HDR)
    assert r.status_code == 409 and r.json()["message"] == UPDATE_RUNNING
    # the script reports success -> the hub exits so launchd relaunches it
    Path(launched["env"]["ILLYHUB_STATUS_FILE"]).write_text("succeeded updated abc -> def\n")
    await wait_for(lambda: rt.test_exits == [0])  # type: ignore[attr-defined]
    status = (await c.get("/api/hub/update/status")).json()
    assert status["state"] == "succeeded" and status["message"] == "updated abc -> def"
    assert status["finished_at"] is not None


async def test_update_rolled_back_exits_and_failed_does_not(client) -> None:
    c, rt = client
    r = await c.post("/api/hub/update/apply", headers=HDR)
    env = rt.test_launcher.calls[0]["env"]  # type: ignore[attr-defined]
    Path(env["ILLYHUB_STATUS_FILE"]).write_text("rolled_back uv sync failed; back on abc1234\n")
    await wait_for(lambda: rt.test_exits == [0])  # type: ignore[attr-defined]
    assert (await c.get("/api/hub/update/status")).json()["state"] == "rolled_back"
    # a new job that fails outright leaves the hub running
    r = await c.post("/api/hub/update/apply", headers=HDR)
    assert r.status_code == 202
    env = rt.test_launcher.calls[1]["env"]  # type: ignore[attr-defined]
    Path(env["ILLYHUB_STATUS_FILE"]).write_text("failed not a git checkout\n")
    await asyncio.sleep(1.2)
    assert rt.test_exits == [0]  # type: ignore[attr-defined]


async def test_update_disabled(client) -> None:
    c, rt = client
    assert rt.update is not None
    rt.update.allow = False
    r = await c.post("/api/hub/update/apply", headers=HDR)
    assert r.status_code == 409 and r.json()["message"] == UPDATE_DISABLED


async def test_dev_scenarios_list_includes_phase8(client) -> None:
    c, _rt = client
    r = await c.post("/api/dev/fake/nope")
    assert r.status_code == 404
    assert {"queue_change", "play_mode_change"} <= set(r.json()["scenarios"])


def test_datetime_import_used() -> None:
    assert datetime.now(UTC).tzinfo is UTC
