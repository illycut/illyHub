from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from illyhub_hub.auth.tidal import TidalAuth
from illyhub_hub.auth.vault import open_vault
from illyhub_hub.content import NeedsLinkError


@dataclass
class Link:
    user_code: str = "ABCDE"
    verification_uri: str = "https://link.tidal.com"
    verification_uri_complete: str = "https://link.tidal.com/ABCDE"
    expires_in: float = 300.0
    interval: float = 2.0


@dataclass
class User:
    username: str | None = "james"
    email: str | None = "james@example.com"
    first_name: str | None = None


@dataclass
class FakeSession:
    """Mimics the tidalapi.Session surface the hub uses. Never touches the network."""

    token_type: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    expiry_time: datetime | None = None
    user: Any = field(default_factory=User)
    accept_tokens: bool = True
    refresh_ok: bool = True
    future: concurrent.futures.Future[Any] = field(default_factory=concurrent.futures.Future)
    calls: list[str] = field(default_factory=list)

    def login_oauth(self):
        self.calls.append("login_oauth")
        return Link(), self.future

    def approve(self) -> None:
        self.token_type, self.access_token = "Bearer", "AT-1"
        self.refresh_token = "RT-1"
        self.expiry_time = datetime.now(UTC) + timedelta(hours=1)
        self.future.set_result(True)

    def load_oauth_session(self, token_type, access_token, refresh_token=None, expiry_time=None):
        self.calls.append("load_oauth_session")
        if not self.accept_tokens:
            return False
        self.token_type, self.access_token = token_type, access_token
        self.refresh_token, self.expiry_time = refresh_token, expiry_time
        return True

    def check_login(self) -> bool:
        return self.access_token is not None

    def token_refresh(self, refresh_token: str) -> bool:
        self.calls.append("token_refresh")
        if not self.refresh_ok:
            return False
        self.access_token = "AT-2"
        self.expiry_time = datetime.now(UTC) + timedelta(hours=1)
        return True


def make(tmp_path: Path, **kw):
    vault = open_vault(None, tmp_path / "k", tmp_path / "v")
    sessions: list[FakeSession] = []

    def factory() -> FakeSession:
        s = FakeSession(**kw)
        sessions.append(s)
        return s

    return TidalAuth(vault, factory=factory), vault, sessions


async def wait_for(pred, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.005)


async def test_link_flow_persists_tokens_and_never_leaks_them(tmp_path: Path) -> None:
    auth, vault, sessions = make(tmp_path)
    assert auth.linked is False and auth.status()["linked"] is False
    started = await auth.start_link()
    assert started == {
        "service": "tidal",
        "user_code": "ABCDE",
        "verification_url": "https://link.tidal.com/ABCDE",
        "expires_in_s": 300,
        "interval_s": 2.0,
    }
    st = auth.status()
    assert st["pending"]["user_code"] == "ABCDE" and st["linked"] is False
    sessions[0].approve()
    await wait_for(lambda: auth.linked)
    st = auth.status()
    assert st["linked"] is True and st["account_name"] == "james" and st["pending"] is None
    assert "AT-1" not in str(st)  # tokens never in the status payload
    stored = vault.get("tidal")
    assert stored["access_token"] == "AT-1" and stored["refresh_token"] == "RT-1"
    await auth.aclose()


async def test_expired_or_failed_link_records_error(tmp_path: Path) -> None:
    auth, _vault, sessions = make(tmp_path)
    await auth.start_link()
    sessions[0].future.set_exception(TimeoutError("code expired"))
    await wait_for(lambda: auth.last_error is not None)
    assert auth.linked is False and auth.pending is None
    assert "did not complete" in auth.last_error
    with pytest.raises(NeedsLinkError):
        auth.session_or_raise()


async def test_restore_from_vault_and_reject_bad_tokens(tmp_path: Path) -> None:
    auth, vault, _s = make(tmp_path)
    assert await auth.restore() is False  # nothing stored
    vault.put(
        "tidal",
        {
            "token_type": "Bearer",
            "access_token": "AT",
            "refresh_token": "RT",
            "expiry_time": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "account_name": "stored-name",
        },
    )
    assert await auth.restore() is True
    assert auth.linked and auth.account_name == "stored-name"
    assert auth.expires_at is not None and auth.expires_at.tzinfo is not None

    bad, vault2, _s2 = make(tmp_path / "b", accept_tokens=False)
    vault2.put("tidal", {"access_token": "AT"})
    assert await bad.restore() is False and "rejected" in (bad.last_error or "")


async def test_unlink_clears_everything(tmp_path: Path) -> None:
    auth, vault, sessions = make(tmp_path)
    await auth.start_link()
    sessions[0].approve()
    await wait_for(lambda: auth.linked)
    await auth.unlink()
    assert auth.linked is False and vault.get("tidal") is None and auth.status()["pending"] is None


async def test_ensure_fresh_refreshes_near_expiry_and_unlinks_on_failure(tmp_path: Path) -> None:
    auth, vault, sessions = make(tmp_path)
    await auth.start_link()
    sessions[0].approve()
    await wait_for(lambda: auth.linked)
    s = sessions[0]
    # Far from expiry: no refresh call.
    await auth.ensure_fresh()
    assert "token_refresh" not in s.calls
    # Within the margin: refresh, persist the new token.
    s.expiry_time = datetime.now(UTC) + timedelta(minutes=1)
    await auth.ensure_fresh()
    assert "token_refresh" in s.calls and vault.get("tidal")["access_token"] == "AT-2"
    # Refresh failure unlinks.
    s.refresh_ok = False
    s.expiry_time = datetime.now(UTC) + timedelta(minutes=1)
    with pytest.raises(NeedsLinkError):
        await auth.ensure_fresh()
    assert auth.linked is False


async def test_pending_code_expires_from_status(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    vault = open_vault(None, tmp_path / "k", tmp_path / "v")
    clock = {"now": now}
    auth = TidalAuth(vault, factory=FakeSession, clock=lambda: clock["now"])
    await auth.start_link()
    assert auth.status()["pending"] is not None
    clock["now"] = now + timedelta(seconds=301)
    assert auth.status()["pending"] is None
    await auth.aclose()


async def test_unlink_during_pending_then_late_approval_stays_unlinked(tmp_path: Path) -> None:
    auth, vault, sessions = make(tmp_path)
    await auth.start_link()
    assert auth.state == "pending"
    await auth.unlink()
    assert auth.state == "unlinked" and len(auth._tasks) == 0  # poller cancelled
    sessions[0].approve()  # Tidal approves after we gave up
    await asyncio.sleep(0.05)
    assert auth.linked is False and vault.get("tidal") is None


async def test_late_approval_of_a_superseded_flow_is_ignored(tmp_path: Path) -> None:
    """The poller must check ``pending`` *before* adopting the session, even if the task was
    not cancelled (e.g. the future completed in the same tick)."""
    auth, vault, sessions = make(tmp_path)
    await auth.start_link()
    first = sessions[0]
    auth.pending = None  # simulate an unlink that raced the completion
    first.approve()
    await asyncio.sleep(0.05)
    assert auth.linked is False and vault.get("tidal") is None


async def test_second_start_cancels_the_first_poller(tmp_path: Path) -> None:
    auth, _vault, sessions = make(tmp_path)
    await auth.start_link()
    await auth.start_link()
    await asyncio.sleep(0)
    live = [t for t in auth._tasks if not t.done()]
    assert len(live) == 1 and len(sessions) == 2
    sessions[0].approve()  # the abandoned flow completing must not link
    await asyncio.sleep(0.05)
    assert auth.linked is False
    sessions[1].approve()
    await wait_for(lambda: auth.linked)
    assert auth.session is sessions[1]
    await auth.aclose()


async def test_concurrent_ensure_fresh_refreshes_once(tmp_path: Path) -> None:
    auth, _vault, sessions = make(tmp_path)
    await auth.start_link()
    sessions[0].approve()
    await wait_for(lambda: auth.linked)
    s = sessions[0]
    s.expiry_time = datetime.now(UTC) + timedelta(minutes=1)
    await asyncio.gather(auth.ensure_fresh(), auth.ensure_fresh(), auth.ensure_fresh())
    assert s.calls.count("token_refresh") == 1


async def test_expired_device_code_while_poller_is_live(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    clock = {"now": now}
    vault = open_vault(None, tmp_path / "k", tmp_path / "v")
    sessions: list[FakeSession] = []

    def factory() -> FakeSession:
        s = FakeSession()
        sessions.append(s)
        return s

    auth = TidalAuth(vault, factory=factory, clock=lambda: clock["now"])
    await auth.start_link()
    clock["now"] = now + timedelta(seconds=301)  # code expired, poller still running
    assert auth.state == "unlinked" and auth.status()["pending"] is None
    sessions[0].future.set_exception(TimeoutError("expired"))
    await wait_for(lambda: auth.last_error is not None)
    assert auth.linked is False
    await auth.aclose()


async def test_restoring_state_until_restore_completes(tmp_path: Path) -> None:
    vault = open_vault(None, tmp_path / "k", tmp_path / "v")
    vault.put("tidal", {"access_token": "AT", "token_type": "Bearer"})
    auth = TidalAuth(vault, factory=FakeSession)
    assert auth.state == "restoring" and auth.status()["state"] == "restoring"
    assert await auth.restore() is True
    assert auth.state == "linked"
    fresh = TidalAuth(open_vault(None, tmp_path / "k2", tmp_path / "v2"), factory=FakeSession)
    assert fresh.state == "unlinked"  # nothing stored → never "restoring"
