"""Phase 7: AirPlay bridge (fake + macOS with a mocked osascript runner) and name matching.

Every ``MacOSAirPlayBridge`` here passes ``euid=501, console_uid=501`` so the suite is hermetic
under sudo/CI; the daemon and console guards get their own tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from illyhub_hub.airplay import (
    AVAILABILITY_CACHE_S,
    LIST_JS,
    RUNNING_JS,
    SET_JS_TEMPLATE,
    AirPlayOutput,
    BridgeUnavailableError,
    FakeAirPlayBridge,
    MacOSAirPlayBridge,
    PandoraSyncController,
    build_bridge,
    default_osa_runner,
    match_outputs,
    set_script,
)
from illyhub_hub.commands import CommandRouter
from illyhub_hub.config import Settings
from illyhub_hub.messages import (
    AIRPLAY_DAEMON,
    AIRPLAY_NEEDS_OUTPUT,
    AIRPLAY_OFF,
    AIRPLAY_RESTORE_FAILED,
)
from illyhub_hub.state import StateStore

DEVICES = [
    {"id": "1", "name": "Hub Mac", "kind": "computer", "selected": True, "active": True},
    {"id": "2", "name": "Living Room Amp", "kind": "AirPlay device", "selected": False},
    {"id": "3", "name": "Kitchen", "kind": "HomePod", "selected": False},
]
USER = {"euid": 501, "console_uid": 501, "platform": "darwin"}


class Runner:
    """Records every invocation; dispatches on the *exact* script text."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], float]] = []
        self.running = "true"
        self.devices = [dict(d) for d in DEVICES]
        self.fail: tuple[int, str] | None = None
        self.timeout = False
        self.oserror: OSError | None = None

    async def __call__(self, argv: list[str], timeout_s: float) -> tuple[int, str, str]:
        self.calls.append((argv, timeout_s))
        if self.timeout:
            raise TimeoutError
        if self.oserror:
            raise self.oserror
        if self.fail:
            return self.fail[0], "", self.fail[1]
        if argv[0] == "open":
            return 0, "", ""
        script = argv[-1]
        if script == RUNNING_JS:
            return 0, self.running, ""
        if script == LIST_JS:
            return 0, json.dumps({"devices": self.devices}), ""
        if script.startswith("const want=new Set(") and script.endswith(
            SET_JS_TEMPLATE.split("__IDS__")[1]
        ):
            ids = json.loads(script[len("const want=new Set(") : script.index(");")])
            assert script == set_script(ids)  # exact match, not a prefix
            for d in self.devices:
                d["selected"] = d["id"] in ids
                d["active"] = d["selected"]
            return 0, json.dumps(self.devices), ""
        raise AssertionError(f"unexpected script {script!r}")


def scripts(runner: Runner) -> list[str]:
    return [argv[-1] for argv, _ in runner.calls if argv[0] == "osascript"]


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# --------------------------------------------------------------------------------------
# macOS bridge
# --------------------------------------------------------------------------------------


async def test_macos_bridge_runs_jxa_and_parses_devices() -> None:
    runner = Runner()
    b = MacOSAirPlayBridge(runner=runner, timeout_s=7, launch_timeout_s=50, **USER)
    assert await b.available() == (True, None)
    outs = await b.list_outputs()
    assert [o.name for o in outs] == ["Hub Mac", "Living Room Amp", "Kitchen"]
    assert outs[0].kind == "computer" and outs[0].kind_label == "This Mac"
    assert outs[2].kind == "HomePod" and outs[2].kind_label == "AirPlay speaker"
    assert "kind_label" in outs[0].model_dump()
    argv, budget = runner.calls[-1]
    assert argv[:4] == ["osascript", "-l", "JavaScript", "-e"] and argv[-1] == LIST_JS
    assert budget == 7
    # available() + list_outputs() = exactly two spawns; the cached availability is reused.
    assert scripts(runner) == [RUNNING_JS, LIST_JS]


async def test_availability_is_cached_for_thirty_seconds() -> None:
    runner, clock = Runner(), Clock()
    b = MacOSAirPlayBridge(runner=runner, clock=clock, **USER)
    await b.available()
    await b.available()
    await b.list_outputs()
    assert scripts(runner).count(RUNNING_JS) == 1
    clock.t += AVAILABILITY_CACHE_S + 1
    await b.available()
    assert scripts(runner).count(RUNNING_JS) == 2
    # A failure invalidates the cache so the next call re-checks.
    runner.fail = (1, "execution error: Music got an error: Application isn't running. (-600)")
    with pytest.raises(BridgeUnavailableError):
        await b.list_outputs()
    runner.fail = None
    assert (await b.available())[0] is True


async def test_macos_bridge_uses_the_launch_budget_when_music_is_not_running() -> None:
    runner = Runner()
    runner.running = "false"
    b = MacOSAirPlayBridge(runner=runner, timeout_s=7, launch_timeout_s=50, **USER)
    await b.list_outputs()
    assert [t for argv, t in runner.calls if argv[-1] == LIST_JS] == [50]


async def test_macos_bridge_set_outputs_selects_wanted_then_deselects_rest() -> None:
    runner = Runner()
    b = MacOSAirPlayBridge(runner=runner, **USER)
    outs = await b.set_outputs(["2", "3"])
    assert [(o.id, o.selected) for o in outs] == [("1", False), ("2", True), ("3", True)]
    script = scripts(runner)[-1]
    assert script == SET_JS_TEMPLATE.replace("__IDS__", '["2", "3"]')
    assert script.index("d.selected=true") < script.index("d.selected=false")
    with pytest.raises(ValueError, match="at least one"):
        await b.set_outputs([])


async def test_set_script_keeps_ids_inside_a_json_string() -> None:
    """A crafted id cannot break out of the JSON literal into the script."""
    evil = '");m.quit();//'
    script = set_script([evil, "2"])
    payload = json.dumps([evil, "2"])
    assert script == SET_JS_TEMPLATE.replace("__IDS__", payload)
    assert script.count("m.quit()") == 1
    body = script[len("const want=new Set(") : script.index(");const m=")]
    assert body == payload and json.loads(body) == [evil, "2"]


@pytest.mark.parametrize(
    ("stderr", "needle"),
    [
        (
            "execution error: Not authorized to send Apple events to Music. (-1743)",
            "Automation permission",
        ),
        (
            "execution error: Music got an error: Application isn't running. (-600)",
            "isn't available",
        ),
        (
            "execution error: Error: A descriptor type mismatch occurred. (-10001)",
            "descriptor mismatch",
        ),
        ("something else entirely", "Music returned an error: something else"),
    ],
)
async def test_macos_bridge_classifies_osascript_errors(stderr: str, needle: str) -> None:
    runner = Runner()
    runner.fail = (1, stderr)
    b = MacOSAirPlayBridge(runner=runner, **USER)
    ok, reason = await b.available()
    assert ok is False and reason and needle in reason
    with pytest.raises(BridgeUnavailableError) as exc:
        await b.list_outputs()
    assert needle in exc.value.reason and b.last_error == exc.value.reason


async def test_macos_bridge_timeout_oserror_and_platform_guards() -> None:
    runner = Runner()
    runner.timeout = True
    b = MacOSAirPlayBridge(runner=runner, timeout_s=5, **USER)
    ok, reason = await b.available()
    assert ok is False and "within 5 seconds" in (reason or "")
    runner.timeout = False
    runner.oserror = PermissionError(13, "Permission denied")
    b2 = MacOSAirPlayBridge(runner=runner, **USER)
    ok, reason = await b2.available()
    assert ok is False and "Permission denied" in (reason or "")
    linux = MacOSAirPlayBridge(runner=Runner(), platform="linux", euid=501, console_uid=501)
    assert (await linux.available())[0] is False
    assert await linux.open_pandora() is False


async def test_daemon_and_console_guards_never_spawn_osascript() -> None:
    runner = Runner()
    root = MacOSAirPlayBridge(runner=runner, platform="darwin", euid=0, console_uid=501)
    assert await root.available() == (False, AIRPLAY_DAEMON)
    assert await root.open_pandora() is False
    nobody = MacOSAirPlayBridge(runner=runner, platform="darwin", euid=501, console_uid=0)
    assert await nobody.available() == (False, AIRPLAY_DAEMON)
    other_user = MacOSAirPlayBridge(runner=runner, platform="darwin", euid=501, console_uid=502)
    assert await other_user.available() == (False, AIRPLAY_DAEMON)  # fast user switching
    assert runner.calls == []
    user = MacOSAirPlayBridge(runner=runner, platform="darwin", euid=501, console_uid=501)
    assert await user.available() == (True, None)
    assert len(runner.calls) == 1


async def test_macos_bridge_open_pandora_uses_open() -> None:
    runner = Runner()
    b = MacOSAirPlayBridge(runner=runner, **USER)
    assert await b.open_pandora() is True
    assert runner.calls[-1][0] == ["open", "https://www.pandora.com"]


async def test_macos_bridge_unreadable_output_is_unavailable() -> None:
    class Garbage(Runner):
        async def __call__(self, argv, timeout_s):  # type: ignore[override]
            self.calls.append((argv, timeout_s))
            return (0, "true", "") if argv[-1] == RUNNING_JS else (0, "not json", "")

    b = MacOSAirPlayBridge(runner=Garbage(), **USER)
    with pytest.raises(BridgeUnavailableError, match="could not read"):
        await b.list_outputs()


async def test_osa_calls_are_serialised() -> None:
    active = 0
    peak = 0

    async def runner(argv, timeout_s):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return 0, ("true" if argv[-1] == RUNNING_JS else json.dumps({"devices": DEVICES})), ""

    b = MacOSAirPlayBridge(runner=runner, **USER)
    await asyncio.gather(b.list_outputs(), b.list_outputs(), b.available())
    assert peak == 1


async def _sleep_children() -> list[str]:
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-P", str(os.getpid()), "-x", "sleep", stdout=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    return out.decode().split()


@pytest.mark.skipif(sys.platform == "win32", reason="posix sleep")
async def test_default_runner_kills_the_child_on_timeout() -> None:
    before = await _sleep_children()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(TimeoutError):
        await default_osa_runner(["sleep", "30"], 0.2)
    assert loop.time() - t0 < 5
    assert await _sleep_children() == before  # killed and reaped: no lingering child, no zombie


async def test_default_runner_returns_output() -> None:
    rc, out, err = await default_osa_runner(["echo", "hi"], 5.0)
    assert (rc, out.strip(), err) == (0, "hi", "")


# --------------------------------------------------------------------------------------
# matching + fake + controller
# --------------------------------------------------------------------------------------


def test_match_outputs_rules() -> None:
    outs = [AirPlayOutput.model_validate(d) for d in DEVICES] + [
        AirPlayOutput(id="4", name="Patio Speaker", kind="AirPlay device"),
        AirPlayOutput(id="5", name="Den", kind="AirPlay device", available=False),
    ]
    got = match_outputs(["living room amp", "Kitchen + Patio", "Den", "Hub Mac"], outs)
    assert [o.id for o in got] == ["2", "3", "4"]
    assert match_outputs(["Office"], outs) == []
    assert match_outputs(["ok"], outs) == []  # too short to match by containment


def test_build_bridge_selects_by_settings(tmp_path) -> None:
    fake = Settings(_env_file=None, fake_devices=True, data_dir=str(tmp_path))
    assert isinstance(build_bridge(fake), FakeAirPlayBridge)
    off = Settings(_env_file=None, data_dir=str(tmp_path))
    assert build_bridge(off) is None
    on = Settings(_env_file=None, airplay_enabled=True, airplay_timeout_s=3, data_dir=str(tmp_path))
    real = build_bridge(on)
    assert isinstance(real, MacOSAirPlayBridge) and real.timeout_s == 3


async def test_controller_off_answers_bridge_unavailable() -> None:
    store = StateStore()
    ctl = PandoraSyncController(store, CommandRouter(store), None, enabled=False)
    assert await ctl.info() == (False, False, AIRPLAY_OFF)
    ack = await ctl.start(output_ids=["x"])
    assert ack.ok is False and ack.error and ack.error.code == "bridge_unavailable"
    assert ack.error.message == AIRPLAY_OFF
    ack = await ctl.set_outputs(["x"])
    assert ack.error and ack.error.code == "bridge_unavailable"
    # Enabled but bridge-less means "no GUI session", never "the flag is off".
    daemon = PandoraSyncController(store, CommandRouter(store), None, enabled=True)
    assert await daemon.info() == (True, False, AIRPLAY_DAEMON)


async def test_controller_refuses_neither_or_both_selectors() -> None:
    store = StateStore()
    ctl = PandoraSyncController(store, CommandRouter(store), FakeAirPlayBridge(), enabled=True)
    for kwargs in ({}, {"side_ids": ["a"], "output_ids": ["b"]}):
        ack = await ctl.start(**kwargs)
        assert ack.error and ack.error.code == "invalid_argument"


async def test_controller_set_outputs_rejects_unknown_ids_before_writing() -> None:
    store = StateStore()
    bridge = FakeAirPlayBridge()
    ctl = PandoraSyncController(store, CommandRouter(store), bridge, enabled=True)
    ack = await ctl.set_outputs(["ap-living", "nope"])
    assert ack.error and ack.error.code == "unknown_target" and "nope" in ack.error.message
    assert bridge.set_calls == []
    ack = await ctl.start(output_ids=["nope"])
    assert ack.error and ack.error.code == "unknown_target" and bridge.set_calls == []


async def test_controller_stop_when_idle_dead_bridge_and_restore_failure() -> None:
    store = StateStore()
    bridge = FakeAirPlayBridge()
    ctl = PandoraSyncController(store, CommandRouter(store), bridge, enabled=True)
    ack = await ctl.stop()
    assert ack.ok is False and ack.error and ack.error.code == "sync_idle"
    ack = await ctl.start(output_ids=["ap-living"])
    assert ack.ok, ack.error
    bridge.unavailable_reason = "Music isn't available on the hub Mac right now."
    ack = await ctl.start(output_ids=["ap-living"])
    assert ack.error and ack.error.code == "bridge_unavailable"
    assert ack.error.message == "Music isn't available on the hub Mac right now."  # no doubling
    ack = await ctl.stop()  # dead bridge: session still ends, restore reported honestly
    assert ack.ok is True and ack.applied == []
    ps = store.state.pandora_sync
    assert ps.active is False and ps.note == AIRPLAY_RESTORE_FAILED


async def test_controller_needs_output_copy_and_lock() -> None:
    store = StateStore()
    bridge = FakeAirPlayBridge()
    ctl = PandoraSyncController(store, CommandRouter(store), bridge, enabled=True)
    ack = await ctl.set_outputs([])
    assert ack.error and ack.error.message == AIRPLAY_NEEDS_OUTPUT
    # Two concurrent starts serialise: the second sees the first's restore point.
    a, b = await asyncio.gather(
        ctl.start(output_ids=["ap-living"]), ctl.start(output_ids=["ap-kitchen"])
    )
    assert a.ok and b.ok
    assert store.state.pandora_sync.previous_output_ids == ["mac"]
