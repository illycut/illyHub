"""AirPlay bridge: the hub Mac as an AirPlay 2 sender for "Pandora Sync" (PRD §3.5 PAN-4).

**Experimental (P2).** Pandora cannot be synchronised across HEOS and Sonos natively (each device
gets its own server-decided track sequence), so the PRD's escape hatch is the hub Mac itself:
play Pandora *on the Mac* and let macOS AirPlay 2 multi-output carry the same audio to both
systems. The bridge scripts the Music app's AirPlay output selection over ``osascript`` (JXA) and
opens Pandora's web player; playback is driven on the Mac, not by the hub
(``docs/spikes/airplay-bridge.md`` has the spike findings and the honest scope).

Two implementations sit behind :class:`AirPlayBridge`: :class:`MacOSAirPlayBridge` (real,
guarded so a missing GUI session, a closed Music app or a denied Automation permission surface as
``bridge_unavailable`` instead of a crash) and :class:`FakeAirPlayBridge` (tests and the PWA in
fake mode). The bridge is off unless ``HUB_AIRPLAY_ENABLED=1``; hubs on other platforms or without
a logged-in user never touch ``osascript``.

**Daemon vs. GUI session.** The production hub is a root LaunchDaemon (the only context macOS 15's
Local Network gate lets reach the LAN). ``osascript`` → Music needs the console user's session,
which a daemon does not have, so the bridge refuses as root or when nobody is at the console and
reports why. The supported path today is a hub process started in the console session
(``HUB_AIRPLAY_ENABLED=1 uv run hub`` on another port); the follow-up is a user-session helper
(``docs/spikes/airplay-bridge.md`` → "Daemon vs. GUI session", illyHub #28).

Verified locally (Apple Silicon, Music 1.6): ``Application("Music").airplayDevices()`` returns
``{id, name, kind, selected, active, available}``; ``device.selected = true/false`` works;
assigning ``currentAirplayDevices`` fails with a descriptor mismatch (-10001), so selection is done
per device. The first call while Music cold-launches can take 30 s or more.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, computed_field

from .commands import Ack, CommandError, CommandRouter, ErrorEnvelope
from .logsetup import correlation_id, get_logger
from .messages import (
    AIRPLAY_DAEMON,
    AIRPLAY_NEEDS_OUTPUT,
    AIRPLAY_NO_MATCH,
    AIRPLAY_NOTHING_TO_RESTORE,
    AIRPLAY_OFF,
    AIRPLAY_RESTORE_FAILED,
    PANDORA_SYNC_BLOCKED_BY_SYNC,
    PANDORA_SYNC_IDLE,
    PANDORA_SYNC_OUTPUTS_ONLY,
    PANDORA_SYNC_STARTED,
    PANDORA_SYNC_STOPPED,
    airplay_unavailable,
)
from .state import PandoraSyncState, StateStore

log = get_logger("airplay")

PANDORA_URL = "https://www.pandora.com"
AVAILABILITY_CACHE_S = 30.0


class AirPlayOutput(BaseModel):
    """One Music AirPlay device as the app shows it. ``kind`` is Music's raw device kind;
    ``kind_label`` is the two-way user label the app renders."""

    id: str
    name: str
    kind: str = "unknown"  # computer | AirPlay device | HomePod | Apple TV | Bluetooth device | ...
    selected: bool = False
    active: bool = False
    available: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def kind_label(self) -> str:
        return "This Mac" if self.kind == "computer" else "AirPlay speaker"


class BridgeUnavailableError(Exception):
    """The bridge cannot talk to Music right now. ``reason`` is a plain sentence for the user."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AirPlayBridge(Protocol):
    async def available(self) -> tuple[bool, str | None]: ...

    async def list_outputs(self) -> list[AirPlayOutput]: ...

    async def set_outputs(self, ids: Sequence[str]) -> list[AirPlayOutput]: ...

    async def open_pandora(self) -> bool: ...


# --------------------------------------------------------------------------------------
# macOS implementation (osascript, JavaScript for Automation)
# --------------------------------------------------------------------------------------

# (rc, stdout, stderr); raises TimeoutError when the budget is exceeded.
OsaRunner = Callable[[list[str], float], Awaitable[tuple[int, str, str]]]

RUNNING_JS = 'Application("Music").running()'

_DEVICE_FIELDS = (
    "({id:String(d.id()),name:d.name(),kind:d.kind(),selected:d.selected(),active:d.active(),"
    "available:d.available()})"
)
LIST_JS = (
    'const m=Application("Music");'
    f"JSON.stringify({{devices:m.airplayDevices().map(d=>{_DEVICE_FIELDS})}})"
)
# Select the wanted devices first, then deselect the rest, so Music is never asked to drop its
# last output (it refuses). Per-device failures (an unavailable speaker) are swallowed in-script.
# ``__IDS__`` is replaced with ``json.dumps(ids)``: the ids only ever appear inside a JSON string
# literal, so a crafted id cannot break out into the script (never str.format: the JS has braces).
SET_JS_TEMPLATE = (
    "const want=new Set(__IDS__);"
    'const m=Application("Music");const ds=m.airplayDevices();'
    "for(const d of ds){try{if(want.has(String(d.id())))d.selected=true;}catch(e){}}"
    "for(const d of ds){try{if(!want.has(String(d.id())))d.selected=false;}catch(e){}}"
    f"JSON.stringify(ds.map(d=>{_DEVICE_FIELDS}))"
)


def set_script(ids: Sequence[str]) -> str:
    return SET_JS_TEMPLATE.replace("__IDS__", json.dumps([str(i) for i in ids]))


def _classify_error(stderr: str, *, timeout_s: float | None = None) -> str:
    """Turn osascript's stderr (or a timeout) into one plain sentence."""
    if timeout_s is not None:
        return (
            f"Music didn't answer within {timeout_s:.0f} seconds. Is a user logged in on the "
            "hub Mac and is Music allowed to open?"
        )
    text = stderr.strip()
    low = text.lower()
    if "(-1743)" in text or "not authorized" in low or "not allowed" in low:
        return (
            "Automation permission for Music is missing. On the hub Mac open System Settings → "
            "Privacy & Security → Automation and allow the hub (or Terminal) to control Music."
        )
    if "(-600)" in text or "isn't running" in low or "can't be found" in low or "(-1728)" in text:
        return "Music isn't available on the hub Mac right now."
    if "(-10001)" in text:
        return "Music rejected the request (descriptor mismatch); the bridge script needs updating."
    return f"Music returned an error: {text[:160]}" if text else "Music returned an error."


async def default_osa_runner(argv: list[str], timeout_s: float) -> tuple[int, str, str]:
    """Run ``argv`` with a hard budget; on timeout the child is killed and reaped (no zombie)."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


class MacOSAirPlayBridge:
    """Drive the Music app's AirPlay outputs with JXA over ``osascript``.

    Every call is serialised (one ``osascript`` at a time), bounded by a timeout, and never raises
    anything but :class:`BridgeUnavailableError` (Music, permission, session or timeout problems)
    or ``ValueError`` (bad input). ``available()`` is cached for :data:`AVAILABILITY_CACHE_S` so a
    ``GET /api/airplay`` costs one spawn. Verified only for "the scripts run and Music answers" on
    the dev Mac; multi-output needs the hub Mac (``ops/RUNBOOK.md`` §5).
    """

    def __init__(
        self,
        *,
        runner: OsaRunner | None = None,
        timeout_s: float = 20.0,
        launch_timeout_s: float = 60.0,
        platform: str | None = None,
        euid: int | None = None,
        console_uid: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._run = runner or default_osa_runner
        self.timeout_s = timeout_s
        self.launch_timeout_s = launch_timeout_s
        self.platform = platform or sys.platform
        self._euid = euid
        self._console_uid = console_uid
        self._clock = clock
        self._lock = asyncio.Lock()
        # (checked_at, ok, reason, music_running)
        self._avail: tuple[float, bool, str | None, bool] | None = None
        self.last_error: str | None = None

    # -- session guard ------------------------------------------------------------

    def _session_reason(self) -> str | None:
        """Why Music scripting cannot work from this process, or ``None``.

        A root daemon has no GUI session; a console owned by root (or by another user, e.g. fast
        user switching) means nobody this process can script for is logged in.
        """
        if not self.platform.startswith("darwin"):
            return "The hub isn't running on macOS."
        euid = self._euid if self._euid is not None else os.geteuid()
        if euid == 0:
            return AIRPLAY_DAEMON
        console = self._console_uid
        if console is None:
            try:
                console = os.stat("/dev/console").st_uid
            except OSError:
                console = None
        if console is not None and console != euid:
            return AIRPLAY_DAEMON
        return None

    # -- plumbing -----------------------------------------------------------------

    async def _osa(self, script: str, *, timeout_s: float | None = None) -> str:
        blocked = self._session_reason()
        if blocked:
            self.last_error = blocked
            raise BridgeUnavailableError(blocked)
        budget = timeout_s or self.timeout_s
        async with self._lock:
            try:
                rc, out, err = await self._run(
                    ["osascript", "-l", "JavaScript", "-e", script], budget
                )
            except TimeoutError as exc:
                self.last_error = _classify_error("", timeout_s=budget)
                raise BridgeUnavailableError(self.last_error) from exc
            except FileNotFoundError as exc:
                self.last_error = "osascript is not installed on the hub."
                raise BridgeUnavailableError(self.last_error) from exc
            except OSError as exc:
                self.last_error = f"Could not run osascript: {exc.strerror or exc}."
                raise BridgeUnavailableError(self.last_error) from exc
        if rc != 0:
            self.last_error = _classify_error(err)
            raise BridgeUnavailableError(self.last_error)
        self.last_error = None
        return out.strip()

    async def _availability(self) -> tuple[bool, str | None, bool]:
        """Cached (ok, reason, music_running); refreshed every :data:`AVAILABILITY_CACHE_S`."""
        now = self._clock()
        if self._avail is not None and now - self._avail[0] < AVAILABILITY_CACHE_S:
            return self._avail[1], self._avail[2], self._avail[3]
        try:
            running = (await self._osa(RUNNING_JS)) == "true"
        except BridgeUnavailableError as exc:
            self._avail = (now, False, exc.reason, False)
            return False, exc.reason, False
        self._avail = (now, True, None, running)
        return True, None, running

    def _invalidate(self) -> None:
        self._avail = None

    @staticmethod
    def _parse_devices(raw: str) -> list[AirPlayOutput]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BridgeUnavailableError(
                "Music returned something the bridge could not read."
            ) from exc
        items = data.get("devices", []) if isinstance(data, dict) else data
        return [AirPlayOutput.model_validate(d) for d in items]

    # -- AirPlayBridge --------------------------------------------------------------

    async def available(self) -> tuple[bool, str | None]:
        ok, reason, _running = await self._availability()
        return ok, reason

    async def list_outputs(self) -> list[AirPlayOutput]:
        ok, reason, running = await self._availability()
        if not ok:
            raise BridgeUnavailableError(reason or "AirPlay is unavailable.")
        budget = self.timeout_s if running else self.launch_timeout_s
        try:
            out = await self._osa(LIST_JS, timeout_s=budget)
        except BridgeUnavailableError:
            self._invalidate()
            raise
        return self._parse_devices(out)

    async def set_outputs(self, ids: Sequence[str]) -> list[AirPlayOutput]:
        wanted = [str(i) for i in ids]
        if not wanted:
            raise ValueError(AIRPLAY_NEEDS_OUTPUT)
        ok, reason, running = await self._availability()
        if not ok:
            raise BridgeUnavailableError(reason or "AirPlay is unavailable.")
        budget = self.timeout_s if running else self.launch_timeout_s
        try:
            out = await self._osa(set_script(wanted), timeout_s=budget)
        except BridgeUnavailableError:
            self._invalidate()
            raise
        return self._parse_devices(out)

    async def open_pandora(self) -> bool:
        """Best effort: open Pandora's web player in the console user's default browser."""
        if self._session_reason():
            return False
        try:
            async with self._lock:
                rc, _out, _err = await self._run(["open", PANDORA_URL], 10.0)
        except (TimeoutError, OSError):
            return False
        return rc == 0


# --------------------------------------------------------------------------------------
# Fake implementation
# --------------------------------------------------------------------------------------


class FakeAirPlayBridge:
    """In-memory bridge for tests and the PWA in fake mode. Outputs are named after the fake
    house's sides so name matching can be exercised end to end."""

    DEFAULT_OUTPUTS: tuple[tuple[str, str, str, bool], ...] = (
        ("mac", "Hub Mac", "computer", True),
        ("ap-living", "Living Room Amp", "AirPlay device", False),
        ("ap-kitchen", "Kitchen + Patio", "AirPlay device", False),
    )

    def __init__(self, outputs: list[AirPlayOutput] | None = None) -> None:
        self.outputs: list[AirPlayOutput] = outputs or [
            AirPlayOutput(id=i, name=n, kind=k, selected=s, active=s)
            for i, n, k, s in self.DEFAULT_OUTPUTS
        ]
        self.unavailable_reason: str | None = None
        self.pandora_opens = 0
        self.set_calls: list[list[str]] = []

    def _guard(self) -> None:
        if self.unavailable_reason:
            raise BridgeUnavailableError(self.unavailable_reason)

    async def available(self) -> tuple[bool, str | None]:
        return (self.unavailable_reason is None, self.unavailable_reason)

    async def list_outputs(self) -> list[AirPlayOutput]:
        self._guard()
        return [o.model_copy() for o in self.outputs]

    async def set_outputs(self, ids: Sequence[str]) -> list[AirPlayOutput]:
        self._guard()
        wanted = [str(i) for i in ids]
        if not wanted:
            raise ValueError(AIRPLAY_NEEDS_OUTPUT)
        self.set_calls.append(wanted)
        for o in self.outputs:
            o.selected = o.id in wanted
            o.active = o.selected
        return [o.model_copy() for o in self.outputs]

    async def open_pandora(self) -> bool:
        self._guard()
        self.pandora_opens += 1
        return True


# --------------------------------------------------------------------------------------
# Name matching + controller
# --------------------------------------------------------------------------------------


def _norm(s: str) -> str:
    return " ".join(s.casefold().split())


def match_outputs(
    room_names: Sequence[str], outputs: Sequence[AirPlayOutput]
) -> list[AirPlayOutput]:
    """Map hub room (side) names to AirPlay outputs.

    Rules, in order, per room name and per ``" + "``-separated part of a grouped side name:
    exact case-insensitive match; then case-insensitive containment either way (min 3 chars).
    The Mac's own ``computer`` output is never matched by name. Duplicates are removed, order
    follows the outputs list.
    """
    candidates = [o for o in outputs if o.kind != "computer" and o.available]
    wanted: dict[str, AirPlayOutput] = {}
    parts: list[str] = []
    for name in room_names:
        parts.append(name)
        parts.extend(p for p in name.split(" + ") if p.strip())
    for part in parts:
        n = _norm(part)
        if len(n) < 3:
            continue
        exact = [o for o in candidates if _norm(o.name) == n]
        hits = exact or [
            o
            for o in candidates
            if len(_norm(o.name)) >= 3 and (n in _norm(o.name) or _norm(o.name) in n)
        ]
        for o in hits:
            wanted.setdefault(o.id, o)
    return [o for o in outputs if o.id in wanted]


class PandoraSyncController:
    """Owns the bridge and the ``HubState.pandora_sync`` slice; builds acks like the sync engine.

    ``bridge`` is ``None`` when ``HUB_AIRPLAY_ENABLED`` is off: every action then answers
    ``bridge_unavailable`` with :data:`AIRPLAY_OFF`, and ``info()`` says so. ``start``, ``stop``
    and ``set_outputs`` are serialised by one lock so two taps cannot interleave selections.
    """

    def __init__(
        self,
        store: StateStore,
        router: CommandRouter,
        bridge: AirPlayBridge | None,
        *,
        enabled: bool,
    ) -> None:
        self.store = store
        self.router = router
        self.bridge = bridge
        self.enabled = enabled
        self._lock = asyncio.Lock()

    # -- status -----------------------------------------------------------------------

    async def info(self) -> tuple[bool, bool, str | None]:
        """(enabled, available, reason) for /api/settings. ``enabled`` is what the flag (or fake
        mode) says; ``available`` is whether Music can be reached right now."""
        if self.bridge is None:
            # Enabled but no bridge can only mean this process cannot own a GUI session.
            return self.enabled, False, (AIRPLAY_DAEMON if self.enabled else AIRPLAY_OFF)
        ok, reason = await self.bridge.available()
        return self.enabled, ok, reason

    async def outputs(self) -> list[AirPlayOutput]:
        bridge = self._require_bridge()
        try:
            return await bridge.list_outputs()
        except BridgeUnavailableError as exc:
            raise CommandError(
                "bridge_unavailable", airplay_unavailable(exc.reason), "airplay"
            ) from exc

    # -- actions ----------------------------------------------------------------------

    async def set_outputs(self, ids: Sequence[str]) -> Ack:
        async with self._lock:
            try:
                outs = await self._set(ids, await self.outputs())
            except CommandError as exc:
                return self._ack("airplay_outputs", error=exc)
        return self._ack("airplay_outputs", applied=[o.id for o in outs if o.selected])

    async def start(
        self,
        *,
        side_ids: Sequence[str] | None = None,
        output_ids: Sequence[str] | None = None,
    ) -> Ack:
        """Route the chosen rooms (or explicit outputs) to AirPlay and open Pandora on the Mac.

        Exactly one of ``side_ids`` / ``output_ids`` must be given (the API enforces it). With
        ``side_ids`` only those rooms' names are matched (grouped names split on " + ", every
        match kept, available outputs only, never the Mac itself); any room without a match refuses
        the whole call and names the rooms.
        """
        async with self._lock:
            try:
                if (not side_ids) == (not output_ids):
                    raise CommandError(
                        "invalid_argument", "Pick rooms or AirPlay outputs, not both.", "airplay"
                    )
                if self.store.state.sync.status not in ("idle", "stopped", "lost"):
                    raise CommandError("sync_active", PANDORA_SYNC_BLOCKED_BY_SYNC, "airplay")
                bridge = self._require_bridge()
                current = await self.outputs()
                sides: list[str] = []
                if output_ids:
                    self._check_known(output_ids, current)
                    wanted = {str(i) for i in output_ids}
                    chosen = [o for o in current if o.id in wanted]
                else:
                    if side_ids is None:  # pragma: no cover - excluded by the XOR check above
                        raise CommandError("invalid_argument", AIRPLAY_NEEDS_OUTPUT, "airplay")
                    state = self.store.state
                    unknown = [sid for sid in side_ids if sid not in state.sides]
                    if unknown:
                        raise CommandError(
                            "unknown_target", f"Nothing is called {unknown[0]!r}.", "airplay"
                        )
                    sides = list(side_ids)
                    chosen: list[AirPlayOutput] = []
                    unmatched: list[str] = []
                    for sid in sides:
                        name = state.sides[sid].name
                        hits = match_outputs([name], current)
                        if not hits:
                            unmatched.append(name)
                        for o in hits:
                            if o.id not in {c.id for c in chosen}:
                                chosen.append(o)
                    if unmatched:
                        raise CommandError(
                            "invalid_argument",
                            AIRPLAY_NO_MATCH.format(rooms=" and ".join(unmatched)),
                            "airplay",
                        )
                previous = self.store.state.pandora_sync
                previous_ids = (
                    previous.previous_output_ids
                    if previous.active
                    else [o.id for o in current if o.selected]
                )
                outs = await self._set([o.id for o in chosen], current)
                try:
                    opened = await bridge.open_pandora()
                except BridgeUnavailableError:
                    opened = False
            except CommandError as exc:
                return self._ack("pandora_sync_start", error=exc)
            note = PANDORA_SYNC_STARTED if opened else PANDORA_SYNC_OUTPUTS_ONLY
            selected = [o for o in outs if o.selected]
            self.store.set_pandora_sync(
                PandoraSyncState(
                    active=True,
                    side_ids=sides,
                    output_ids=[o.id for o in selected],
                    outputs=[o.name for o in selected],
                    previous_output_ids=previous_ids,
                    started_at=datetime.now(UTC),
                    note=note,
                )
            )
        log.info(
            "pandora sync started",
            extra={"extra": {"outputs": [o.name for o in selected], "opened": opened}},
        )
        return self._ack("pandora_sync_start", applied=[o.id for o in selected])

    async def stop(self) -> Ack:
        async with self._lock:
            current = self.store.state.pandora_sync
            if not current.active:
                return self._ack(
                    "pandora_sync_stop",
                    error=CommandError("sync_idle", PANDORA_SYNC_IDLE, "airplay"),
                )
            restored: list[str] = []
            note = PANDORA_SYNC_STOPPED
            if not current.previous_output_ids:
                note = AIRPLAY_NOTHING_TO_RESTORE
            else:
                try:
                    outs = await self._set(current.previous_output_ids, None)
                    restored = [o.id for o in outs if o.selected]
                except CommandError as exc:
                    # The bridge is gone; still end the session so the UI is never stuck.
                    log.warning(
                        "pandora sync stop: could not restore outputs",
                        extra={"extra": {"error": exc.message}},
                    )
                    note = AIRPLAY_RESTORE_FAILED
            self.store.set_pandora_sync(PandoraSyncState(active=False, note=note))
        return self._ack("pandora_sync_stop", applied=restored)

    # -- helpers ----------------------------------------------------------------------

    def _require_bridge(self) -> AirPlayBridge:
        if self.bridge is None:
            raise CommandError("bridge_unavailable", AIRPLAY_OFF, "airplay")
        return self.bridge

    @staticmethod
    def _check_known(ids: Sequence[str], current: Sequence[AirPlayOutput]) -> None:
        known = {o.id for o in current}
        missing = [str(i) for i in ids if str(i) not in known]
        if missing:
            raise CommandError(
                "unknown_target", f"No AirPlay output is called {missing[0]!r}.", "airplay"
            )

    async def _set(
        self, ids: Sequence[str], current: Sequence[AirPlayOutput] | None
    ) -> list[AirPlayOutput]:
        """Select exactly ``ids``. ``current`` (when given) is the fresh output list used to reject
        unknown ids before anything is written; ``None`` skips the check (restore path)."""
        bridge = self._require_bridge()
        if not ids:
            raise CommandError("invalid_argument", AIRPLAY_NEEDS_OUTPUT, "airplay")
        if current is not None:
            self._check_known(ids, current)
        try:
            return await bridge.set_outputs(ids)
        except ValueError as exc:
            raise CommandError("invalid_argument", str(exc), "airplay") from exc
        except BridgeUnavailableError as exc:
            raise CommandError(
                "bridge_unavailable", airplay_unavailable(exc.reason), "airplay"
            ) from exc

    def _ack(
        self, action: str, *, error: CommandError | None = None, applied: list[str] | None = None
    ) -> Ack:
        cid = correlation_id.get() or "-"
        ack = Ack(
            correlation_id=cid,
            ok=error is None,
            action=action,
            target="airplay",
            state_version=self.store.state.version,
            error=(
                ErrorEnvelope(
                    code=error.code,
                    message=error.message,
                    target=error.target or "airplay",
                    correlation_id=cid,
                )
                if error
                else None
            ),
            applied=applied or [],
        )
        log.info(
            "command",
            extra={
                "extra": {
                    "action": action,
                    "target": "airplay",
                    "ok": ack.ok,
                    **({"code": error.code} if error else {}),
                }
            },
        )
        self.router._broadcast(ack)  # noqa: SLF001 - same package; one ack per call
        return ack


def build_bridge(settings: Any) -> AirPlayBridge | None:
    """Fakes in fake mode; the real bridge only when ``HUB_AIRPLAY_ENABLED=1``; else ``None``."""
    if getattr(settings, "fake_devices", False):
        return FakeAirPlayBridge()
    if not getattr(settings, "airplay_enabled", False):
        return None
    return MacOSAirPlayBridge(
        timeout_s=settings.airplay_timeout_s, launch_timeout_s=settings.airplay_launch_timeout_s
    )
