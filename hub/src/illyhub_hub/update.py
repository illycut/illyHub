"""Self-update for the hub (PRD SET-2, ai-dev #72).

``check`` runs ``git fetch`` + ``git rev-list`` in the checkout the hub runs from and compares
HEAD to the upstream of the current branch (falling back to ``HUB_UPDATE_BRANCH`` when the branch
has no upstream). ``apply`` launches ``ops/update.sh`` **detached** (own session, output to a log
under ``HUB_DATA_DIR/update/``) and returns a job id; the script fast-forwards, syncs
dependencies, rebuilds the PWA when npm is on the daemon's PATH, and writes its state. The hub
exits itself when the state is ``succeeded`` or ``rolled_back`` (launchd relaunches it); the
script never kicks launchd from an API-launched run. ``status`` reads the job's state file and a
tail of the log, off the event loop.

Safety (review decisions): the endpoint is **off by default** (``HUB_ALLOW_UPDATE``); it refuses
when the ``origin`` remote is not an ``https://`` or ``ssh://`` URL; every apply logs the remote
and the target commit at WARNING; a ``running`` job whose child is gone and that is older than
:data:`STALE_AFTER_S` is reported ``failed`` so a crash mid-update never wedges the endpoint.

Everything that shells out is behind an injectable runner so the unit tests never touch git or
the filesystem beyond a temp dir; ``ops/update.sh`` itself has bash-level tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .logsetup import get_logger
from .messages import UPDATE_CHECK_FAILED, UPDATE_REMOTE_REFUSED, UPDATE_STALE

log = get_logger("update")

UpdateState = Literal["idle", "running", "succeeded", "up_to_date", "failed", "rolled_back"]
Runner = Callable[[list[str], Path, float], Awaitable[tuple[int, str, str]]]
Launcher = Callable[[list[str], Path, Path, dict[str, str]], Awaitable[int]]

CHECK_TIMEOUT_S = 20.0
LOG_TAIL_BYTES = 8 * 1024
LOG_TAIL_LINES = 40
STALE_AFTER_S = 15 * 60
KEEP_JOB_LOGS = 10
TERMINAL: frozenset[str] = frozenset({"succeeded", "up_to_date", "failed", "rolled_back"})
ALLOWED_REMOTE_SCHEMES = ("https://", "ssh://", "git@")


class UpdateVersion(BaseModel):
    version: str
    commit: str | None = None
    branch: str | None = None


class RemoteVersion(BaseModel):
    commit: str | None = None
    ahead_by: int = 0
    summary: list[str] = Field(default_factory=list)  # newest-first one-line subjects
    tracking: str | None = None  # the branch compared against (on origin), e.g. "main"


class UpdateCheck(BaseModel):
    current: UpdateVersion
    remote: RemoteVersion
    available: bool = False
    last_checked_at: datetime
    error: str | None = None


class UpdateStatus(BaseModel):
    state: UpdateState = "idle"
    job_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    log_tail: list[str] = Field(default_factory=list)
    message: str | None = None


class UpdateJob(BaseModel):
    job_id: str
    state: UpdateState = "running"
    started_at: datetime
    log_path: str
    status_path: str
    pid: int | None = None


async def default_runner(argv: list[str], cwd: Path, timeout_s: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _popen_detached(argv: list[str], cwd: Path, log_path: Path, env: dict[str, str]) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as fh:
        proc = subprocess.Popen(  # noqa: S603 - argv is fixed by the hub, not user input
            argv,
            cwd=str(cwd),
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, **env},
        )
    return proc.pid


async def default_launcher(argv: list[str], cwd: Path, log_path: Path, env: dict[str, str]) -> int:
    """Start the update script in its own session so it survives the hub exiting."""
    return await asyncio.to_thread(_popen_detached, argv, cwd, log_path, env)


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _tail(path: Path, max_bytes: int = LOG_TAIL_BYTES, lines: int = LOG_TAIL_LINES) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return []
    text = data.decode(errors="replace")
    if len(data) == max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]  # drop a partial first line
    return text.splitlines()[-lines:]


def remote_allowed(url: str) -> bool:
    return url.strip().startswith(ALLOWED_REMOTE_SCHEMES)


class UpdateManager:
    def __init__(
        self,
        repo_dir: Path,
        data_dir: Path,
        *,
        version: str,
        allow: bool = False,
        fallback_branch: str = "main",
        runner: Runner | None = None,
        launcher: Launcher | None = None,
        clock: Callable[[], datetime] | None = None,
        pid_alive_fn: Callable[[int | None], bool] = pid_alive,
    ) -> None:
        self.repo_dir = repo_dir
        self.dir = data_dir / "update"
        self.version = version
        self.allow = allow
        self.fallback_branch = fallback_branch
        self._run = runner or default_runner
        self._launch = launcher or default_launcher
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pid_alive = pid_alive_fn
        self._last_check: UpdateCheck | None = None
        self._job: UpdateJob | None = self._load_job()
        self._lock = asyncio.Lock()

    # -- check ---------------------------------------------------------------------------

    async def _git(self, *args: str) -> str:
        code, out, err = await self._run(["git", *args], self.repo_dir, CHECK_TIMEOUT_S)
        if code != 0:
            raise RuntimeError((err or out).strip() or f"git {args[0]} failed")
        return out.strip()

    async def _tracking_branch(self, branch: str) -> str:
        """``branch`` when it exists on origin, else the configured fallback branch."""
        try:
            await self._git("rev-parse", "--verify", "--quiet", f"origin/{branch}")
            return branch
        except RuntimeError:
            return self.fallback_branch

    async def check(self) -> UpdateCheck:
        now = self._clock()
        current = UpdateVersion(version=self.version)
        try:
            current.commit = await self._git("rev-parse", "--short", "HEAD")
            current.branch = await self._git("rev-parse", "--abbrev-ref", "HEAD")
            await self._git("fetch", "--quiet", "origin")
            branch = current.branch if current.branch != "HEAD" else self.fallback_branch
            tracking = await self._tracking_branch(branch)
            upstream = f"origin/{tracking}"
            remote_commit = await self._git("rev-parse", "--short", upstream)
            ahead = int(await self._git("rev-list", "--count", f"HEAD..{upstream}") or 0)
            summary: list[str] = []
            if ahead:
                lines = await self._git("log", "--format=%s", f"HEAD..{upstream}", "-n", "10")
                summary = lines.splitlines()
            result = UpdateCheck(
                current=current,
                remote=RemoteVersion(
                    commit=remote_commit, ahead_by=ahead, summary=summary, tracking=tracking
                ),
                available=ahead > 0,
                last_checked_at=now,
            )
        except (RuntimeError, TimeoutError, OSError, ValueError) as exc:
            # One sentence for the screen; the raw git text goes to the log only.
            result = UpdateCheck(
                current=current,
                remote=RemoteVersion(),
                last_checked_at=now,
                error=UPDATE_CHECK_FAILED,
            )
            log.warning("update check failed", extra={"extra": {"error": str(exc)}})
        self._last_check = result
        return result

    # -- apply ---------------------------------------------------------------------------

    def _job_path(self) -> Path:
        return self.dir / "job.json"

    def _load_job(self) -> UpdateJob | None:
        try:
            return UpdateJob.model_validate(json.loads(self._job_path().read_text()))
        except (OSError, ValueError):
            return None

    def _save_job(self, job: UpdateJob) -> None:
        _write_atomic(self._job_path(), job.model_dump_json())

    def _prune_logs(self) -> None:
        logs = sorted(self.dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in logs[KEEP_JOB_LOGS:]:
            for path in (old, old.with_suffix(".status")):
                try:
                    path.unlink()
                except OSError:
                    pass

    async def apply(self) -> UpdateJob:
        """Launch ``ops/update.sh`` detached. Raises ``PermissionError`` when disabled or when the
        remote is refused, ``RuntimeError`` when a job is still running."""
        if not self.allow:
            raise PermissionError("Updates are disabled on this hub (HUB_ALLOW_UPDATE=0).")
        async with self._lock:
            status = await self.status()
            if status.state == "running":
                raise RuntimeError("An update is already running.")
            try:
                remote = await self._git("remote", "get-url", "origin")
            except RuntimeError as exc:
                raise PermissionError(f"{UPDATE_REMOTE_REFUSED} ({exc})") from exc
            if not remote_allowed(remote):
                raise PermissionError(UPDATE_REMOTE_REFUSED)
            try:
                target = await self._git("rev-parse", "--short", "origin/HEAD")
            except RuntimeError:
                target = "?"
            job_id = uuid.uuid4().hex[:8]
            now = self._clock()
            log_path = self.dir / f"{job_id}.log"
            status_path = self.dir / f"{job_id}.status"
            job = UpdateJob(
                job_id=job_id,
                started_at=now,
                log_path=str(log_path),
                status_path=str(status_path),
            )
            await asyncio.to_thread(self._save_job, job)
            script = self.repo_dir / "ops" / "update.sh"
            log.warning(
                "update apply",
                extra={
                    "extra": {
                        "job": job_id,
                        "remote": remote,
                        "target": target,
                        "branch": self.fallback_branch,
                        "log": str(log_path),
                    }
                },
            )
            job.pid = await self._launch(
                ["/bin/bash", str(script)],
                self.repo_dir,
                log_path,
                {
                    "ILLYHUB_STATUS_FILE": str(status_path),
                    "ILLYHUB_JOB_ID": job_id,
                    "ILLYHUB_UPDATE_RESTART": "0",  # the hub exits itself; no kickstart SIGKILL
                    "HUB_UPDATE_BRANCH": self.fallback_branch,
                },
            )
            await asyncio.to_thread(self._save_job, job)
            await asyncio.to_thread(self._prune_logs)
            self._job = job
            return job

    def _read_status(self, job: UpdateJob) -> UpdateStatus:
        state: UpdateState = "running"
        message: str | None = None
        finished: datetime | None = None
        try:
            raw = Path(job.status_path).read_text().strip()
        except OSError:
            raw = ""
        if raw:
            head, _, rest = raw.partition(" ")
            if head in TERMINAL:
                state = head  # type: ignore[assignment]
                message = rest.strip() or None
                try:
                    finished = datetime.fromtimestamp(Path(job.status_path).stat().st_mtime, UTC)
                except OSError:
                    finished = None
        if state == "running":
            age = (self._clock() - job.started_at).total_seconds()
            if not self._pid_alive(job.pid) and age > STALE_AFTER_S:
                state, message = "failed", UPDATE_STALE
        return UpdateStatus(
            state=state,
            job_id=job.job_id,
            started_at=job.started_at,
            finished_at=finished,
            log_tail=_tail(Path(job.log_path)),
            message=message,
        )

    def mark_failed(self, job_id: str, message: str) -> None:
        """Write a terminal ``failed`` state for ``job_id`` (watch timeout)."""
        job = self._job or self._load_job()
        if job is None or job.job_id != job_id:
            return
        _write_atomic(Path(job.status_path), f"failed {message}\n")

    async def status(self) -> UpdateStatus:
        job = self._job or self._load_job()
        if job is None:
            return UpdateStatus()
        return await asyncio.to_thread(self._read_status, job)

    def last_check(self) -> UpdateCheck | None:
        return self._last_check


__all__ = [
    "TERMINAL",
    "UpdateCheck",
    "UpdateJob",
    "UpdateManager",
    "UpdateStatus",
    "UpdateVersion",
    "default_launcher",
    "default_runner",
    "pid_alive",
    "remote_allowed",
]
