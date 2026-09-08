"""ops/update.sh against a throwaway git repository: up to date, fast-forward success, and
rollback when the dependency sync fails. ``uv``/``npm`` are shimmed on PATH so nothing real runs."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "ops" / "update.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repos(
    tmp_path: Path,
    *,
    uv_fail_first: bool = False,
    uv_fail_always: bool = False,
    npm: str | None = None,
) -> tuple[Path, Path, Path]:
    """origin (bare), a clone with ops/update.sh + hub/, and a bin dir with a fake uv.

    With ``uv_fail_first`` the fake uv exits 1 for the update's sync (both the --frozen attempt
    and the retry) and 0 for the rollback's sync, which is the rollback path the hub relies on."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    git(work, "config", "user.email", "t@t")
    git(work, "config", "user.name", "t")
    (work / "ops").mkdir()
    shutil.copy(SCRIPT, work / "ops" / "update.sh")
    (work / "hub").mkdir()
    (work / "hub" / "pyproject.toml").write_text("[project]\nname='x'\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "v1")
    git(work, "push", "-q", "-u", "origin", "main")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # The script retries `uv sync --frozen` as plain `uv sync`, so "fail the update's sync" means
    # failing the first two calls; the rollback's own two calls then succeed.
    counter = tmp_path / "uv-calls"
    lines = ["#!/bin/bash", f'echo fake-uv "$@" >> "{tmp_path}/uv.log"']
    if uv_fail_first:
        lines.append(f'n=$(cat "{counter}" 2>/dev/null || echo 0); echo $((n + 1)) > "{counter}"')
        lines.append('[[ "$n" -ge 2 ]] || { echo failing >&2; exit 1; }')
    if uv_fail_always:
        lines.append("echo failing >&2; exit 1")
    lines.append("exit 0")
    (bin_dir / "uv").write_text("\n".join(lines) + "\n")
    (bin_dir / "uv").chmod(0o755)
    if npm is not None:
        # "ok" builds; "fail" fails `npm run build` (after a successful `npm ci`).
        body = ["#!/bin/bash", f'echo fake-npm "$@" >> "{tmp_path}/npm.log"']
        if npm == "fail":
            body.append('[[ "$1" == "run" ]] && { echo build failed >&2; exit 1; }')
        body.append("exit 0")
        (bin_dir / "npm").write_text("\n".join(body) + "\n")
        (bin_dir / "npm").chmod(0o755)
        (work / "app").mkdir(exist_ok=True)
        (work / "app" / "package.json").write_text("{}")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "app")
        git(work, "push", "-q", "origin", "main")
    return origin, work, bin_dir


def run_update(work: Path, bin_dir: Path, status: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "ILLYHUB_STATUS_FILE": str(status),
        "ILLYHUB_JOB_ID": "t1",
        "ILLYHUB_UPDATE_RESTART": "0",
        "HOME": str(work.parent),
    }
    return subprocess.run(
        ["/bin/bash", str(work / "ops" / "update.sh")],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def push_new_commit(tmp_path: Path, origin: Path) -> str:
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    git(other, "config", "user.email", "t@t")
    git(other, "config", "user.name", "t")
    (other / "NEW.txt").write_text("v2\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "v2")
    git(other, "push", "-q", "origin", "main")
    return git(other, "rev-parse", "--short", "HEAD")


def test_update_script_up_to_date(tmp_path: Path) -> None:
    _origin, work, bin_dir = make_repos(tmp_path)
    status = tmp_path / "s.status"
    r = run_update(work, bin_dir, status)
    assert r.returncode == 0, r.stdout + r.stderr
    assert status.read_text().startswith("up_to_date already up to date")


def test_update_script_fast_forwards_and_syncs(tmp_path: Path) -> None:
    origin, work, bin_dir = make_repos(tmp_path)
    new = push_new_commit(tmp_path, origin)
    status = tmp_path / "s.status"
    r = run_update(work, bin_dir, status)
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(work, "rev-parse", "--short", "HEAD") == new
    text = status.read_text()
    assert text.startswith("succeeded updated") and text.strip().endswith(new)
    assert "fake-uv sync" in (tmp_path / "uv.log").read_text()
    assert "npm not found" in r.stdout  # no app/package.json in the temp repo


def test_update_script_rolls_back_when_sync_fails(tmp_path: Path) -> None:
    origin, work, bin_dir = make_repos(tmp_path, uv_fail_first=True)
    before = git(work, "rev-parse", "--short", "HEAD")
    push_new_commit(tmp_path, origin)
    status = tmp_path / "s.status"
    r = run_update(work, bin_dir, status)
    assert r.returncode == 1
    text = status.read_text()
    assert text.startswith("rolled_back uv sync failed")
    assert git(work, "rev-parse", "--short", "HEAD") == before
    assert not (work / "NEW.txt").exists()


def test_update_script_refuses_a_dirty_tree_and_preserves_it(tmp_path: Path) -> None:
    origin, work, bin_dir = make_repos(tmp_path)
    push_new_commit(tmp_path, origin)
    (work / "hub" / "pyproject.toml").write_text("[project]\nname='edited locally'\n")
    before = git(work, "rev-parse", "HEAD")
    status = tmp_path / "s.status"
    r = run_update(work, bin_dir, status)
    assert r.returncode == 2
    assert status.read_text().startswith("failed the checkout has uncommitted changes")
    assert git(work, "rev-parse", "HEAD") == before
    assert "edited locally" in (work / "hub" / "pyproject.toml").read_text()  # nothing reset
    assert not (tmp_path / "uv.log").exists()  # never got as far as syncing


def test_update_script_refuses_a_diverged_branch_and_preserves_it(tmp_path: Path) -> None:
    origin, work, bin_dir = make_repos(tmp_path)
    push_new_commit(tmp_path, origin)
    (work / "LOCAL.txt").write_text("local\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "local commit")
    before = git(work, "rev-parse", "HEAD")
    status = tmp_path / "s.status"
    r = run_update(work, bin_dir, status)
    assert r.returncode == 1
    text = status.read_text()
    assert text.startswith("failed git merge --ff-only origin/main refused")
    assert git(work, "rev-parse", "HEAD") == before and (work / "LOCAL.txt").exists()
    assert not (tmp_path / "uv.log").exists()


def test_update_script_refuses_missing_uv(tmp_path: Path) -> None:
    origin, work, bin_dir = make_repos(tmp_path)
    (bin_dir / "uv").unlink()
    status = tmp_path / "s.status"
    r = run_update(work, bin_dir, status)
    assert r.returncode == 2 and status.read_text().startswith("failed uv not found on PATH")


def test_update_script_builds_the_app_when_npm_is_present(tmp_path: Path) -> None:
    origin, work, bin_dir = make_repos(tmp_path, npm="ok")
    new = push_new_commit(tmp_path, origin)
    status = tmp_path / "s.status"
    r = run_update(work, bin_dir, status)
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(work, "rev-parse", "--short", "HEAD") == new
    npm_log = (tmp_path / "npm.log").read_text()
    assert "fake-npm ci" in npm_log and "fake-npm run build" in npm_log
    assert "app built" in r.stdout


def test_update_script_rolls_back_when_the_app_build_fails(tmp_path: Path) -> None:
    origin, work, bin_dir = make_repos(tmp_path, npm="fail")
    before = git(work, "rev-parse", "--short", "HEAD")
    push_new_commit(tmp_path, origin)
    status = tmp_path / "s.status"
    r = run_update(work, bin_dir, status)
    assert r.returncode == 1
    assert status.read_text().startswith("rolled_back app build failed")
    assert git(work, "rev-parse", "--short", "HEAD") == before


def test_update_script_reports_failed_when_rollback_also_fails(tmp_path: Path) -> None:
    origin, work, bin_dir = make_repos(tmp_path, uv_fail_always=True)
    before = git(work, "rev-parse", "HEAD")
    push_new_commit(tmp_path, origin)
    status = tmp_path / "s.status"
    r = run_update(work, bin_dir, status)
    assert r.returncode == 1
    text = status.read_text()
    assert text.startswith("failed uv sync failed") and "rollback" in text and "also failed" in text
    # git itself was reset to the recorded commit even though the re-sync failed
    assert git(work, "rev-parse", "HEAD") == before


def test_update_script_status_writes_are_atomic(tmp_path: Path) -> None:
    _origin, work, bin_dir = make_repos(tmp_path)
    status = tmp_path / "s.status"
    run_update(work, bin_dir, status)
    assert not (tmp_path / "s.status.tmp").exists()
    assert not list(tmp_path.glob("s.status.tmp.*"))
