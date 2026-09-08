"""YouTube Music device-code auth against fake ytmusicapi credentials (no network)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from illyhub_hub.auth.vault import open_vault
from illyhub_hub.auth.ytmusic import MISSING_CLIENT_MSG, YTMusicAuth
from illyhub_hub.content import NeedsLinkError


@dataclass
class FakeCreds:
    """Google device-flow responses: ``pending`` polls of authorization_pending, then a token."""

    pending: int = 1
    outcome: str = "token"  # token | access_denied | expired_token | raise
    code_response: dict[str, Any] | None = None
    polls: int = 0
    refreshes: int = 0
    refresh_ok: bool = True
    slow_down_first: bool = False

    def get_code(self) -> dict[str, Any]:
        return self.code_response or {
            "device_code": "DEV-1",
            "user_code": "ABCD-EFGH",
            "verification_url": "https://www.google.com/device",
            "expires_in": 1800,
            "interval": 0.01,
        }

    def token_from_code(self, device_code: str) -> dict[str, Any]:
        assert device_code == "DEV-1"
        self.polls += 1
        if self.slow_down_first and self.polls == 1:
            return {"error": "slow_down"}
        if self.polls <= self.pending:
            return {"error": "authorization_pending"}
        if self.outcome == "raise":
            raise RuntimeError("boom")
        if self.outcome != "token":
            return {"error": self.outcome}
        return {
            "access_token": f"AT-{self.polls}",
            "refresh_token": "RT-1",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/youtube",
            "token_type": "Bearer",
        }

    def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        self.refreshes += 1
        assert refresh_token == "RT-1"
        if not self.refresh_ok:
            return {"error": "invalid_grant"}
        return {"access_token": f"AT-R{self.refreshes}", "expires_in": 3600}


@dataclass
class FakeClient:
    token: dict[str, Any]
    name: str | None = "James"
    fail: bool = False
    calls: list[str] = field(default_factory=list)

    def get_account_info(self) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("client broken")
        return {"accountName": self.name}


def make(tmp_path: Path, creds: FakeCreds | None = None, *, configured: bool = True, **kw: Any):
    vault = open_vault(None, tmp_path / "k", tmp_path / "v")
    creds = creds or FakeCreds()
    clients: list[FakeClient] = []

    def client_factory(token: dict[str, Any], c: Any) -> FakeClient:
        cl = FakeClient(token, **kw)
        clients.append(cl)
        if cl.fail:
            raise RuntimeError("client broken")
        return cl

    auth = YTMusicAuth(
        vault,
        client_id="cid" if configured else None,
        client_secret="sec" if configured else None,
        credentials_factory=lambda cid, sec: creds,
        client_factory=client_factory,
    )
    return auth, vault, creds, clients


async def wait_for(pred, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.005)


async def test_link_flow_pending_then_linked_persists_token(tmp_path: Path) -> None:
    auth, vault, creds, clients = make(tmp_path, FakeCreds(pending=2, slow_down_first=True))
    auth.slow_down_step_s = 0.01
    started = await auth.start_link()
    assert started["user_code"] == "ABCD-EFGH" and started["expires_in_s"] == 1800
    assert auth.status()["state"] == "pending"
    assert auth.status()["pending"]["verification_url"] == "https://www.google.com/device"
    await wait_for(lambda: auth.linked)
    assert creds.polls == 3  # slow_down, pending, token
    assert auth.status()["state"] == "linked" and auth.account_name == "James"
    stored = vault.get("ytmusic")
    assert stored["token"]["access_token"] == "AT-3" and stored["token"]["refresh_token"] == "RT-1"
    assert stored["token"]["expires_at"] > int(time.time())
    assert auth.client_or_raise() is clients[-1]
    await auth.aclose()


async def test_unlink_during_pending_ignores_late_approval(tmp_path: Path) -> None:
    auth, vault, creds, _ = make(tmp_path, FakeCreds(pending=50))
    await auth.start_link()
    await wait_for(lambda: creds.polls >= 2)
    await auth.unlink()
    assert auth.status()["state"] == "unlinked" and vault.get("ytmusic") is None
    await asyncio.sleep(0.05)
    assert not auth.linked and vault.get("ytmusic") is None
    assert not auth._tasks or all(t.done() for t in auth._tasks)


async def test_second_start_supersedes_first_poller(tmp_path: Path) -> None:
    auth, _vault, creds, _ = make(tmp_path, FakeCreds(pending=100))
    await auth.start_link()
    await auth.start_link()
    live = [t for t in auth._tasks if not t.done()]
    assert len(live) == 1
    await auth.aclose()


@pytest.mark.parametrize("outcome", ["access_denied", "expired_token", "raise"])
async def test_refused_or_failed_link_reports_error_and_clears_pending(
    tmp_path: Path, outcome: str
) -> None:
    auth, vault, _creds, _ = make(tmp_path, FakeCreds(pending=0, outcome=outcome))
    await auth.start_link()
    await wait_for(lambda: auth.pending is None)
    status = auth.status()
    assert status["state"] == "unlinked" and status["last_error"]
    assert "YouTube Music" in status["last_error"]
    assert vault.get("ytmusic") is None


async def test_expired_code_while_polling(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    clock = {"t": now}
    auth, _vault, creds, _ = make(tmp_path, FakeCreds(pending=1000))
    auth.clock = lambda: clock["t"]
    await auth.start_link()
    clock["t"] = now + timedelta(seconds=1801)
    await wait_for(lambda: auth.pending is None)
    assert "expired" in (auth.status()["last_error"] or "")


async def test_unconfigured_client_id_is_a_needs_link_with_instructions(tmp_path: Path) -> None:
    auth, _vault, _creds, _ = make(tmp_path, configured=False)
    with pytest.raises(NeedsLinkError) as ei:
        await auth.start_link()
    assert ei.value.message == MISSING_CLIENT_MSG
    assert auth.status()["last_error"] == MISSING_CLIENT_MSG
    assert auth.status()["state"] == "unlinked"


async def test_restore_from_vault_and_restoring_state(tmp_path: Path) -> None:
    auth, vault, _creds, _ = make(tmp_path)
    await auth.start_link()
    await wait_for(lambda: auth.linked)
    await auth.aclose()
    # Fresh process: tokens stored → "restoring" until validated.
    auth2, _, _, clients = make(tmp_path)
    assert auth2.state == "restoring"
    assert await auth2.restore() is True
    assert auth2.linked and auth2.account_name == "James"
    # Unconfigured process with stored tokens cannot restore; says why.
    auth3, _, _, _ = make(tmp_path, configured=False)
    assert await auth3.restore() is False
    assert auth3.last_error == MISSING_CLIENT_MSG


async def test_restore_rejects_broken_client(tmp_path: Path) -> None:
    auth, vault, _creds, _ = make(tmp_path)
    await auth.start_link()
    await wait_for(lambda: auth.linked)
    auth2, _, _, _ = make(tmp_path, fail=True)
    assert await auth2.restore() is False
    assert "restore failed" in (auth2.last_error or "")


async def test_ensure_fresh_refreshes_once_under_concurrency_and_persists(tmp_path: Path) -> None:
    auth, vault, creds, clients = make(tmp_path)
    await auth.start_link()
    await wait_for(lambda: auth.linked)
    assert auth.token is not None
    auth.token["expires_at"] = int(time.time()) + 60  # inside the 5-minute margin
    results = await asyncio.gather(*(auth.ensure_fresh() for _ in range(3)))
    assert creds.refreshes == 1
    assert all(r is results[0] for r in results)
    assert auth.token["access_token"] == "AT-R1"
    assert vault.get("ytmusic")["token"]["access_token"] == "AT-R1"
    # Far from expiry → no refresh.
    auth.token["expires_at"] = int(time.time()) + 7200
    await auth.ensure_fresh()
    assert creds.refreshes == 1


async def test_refresh_failure_unlinks_with_needs_link(tmp_path: Path) -> None:
    auth, _vault, creds, _ = make(tmp_path, FakeCreds(refresh_ok=False))
    await auth.start_link()
    await wait_for(lambda: auth.linked)
    assert auth.token is not None
    auth.token["expires_at"] = int(time.time()) + 10
    with pytest.raises(NeedsLinkError) as ei:
        await auth.ensure_fresh()
    assert ei.value.message == "YouTube Music session expired. Link it again."
    assert not auth.linked and auth.last_error == ei.value.message
    assert _vault.get("ytmusic") is None  # dead token removed: a restart cannot re-adopt it
    with pytest.raises(NeedsLinkError):
        auth.client_or_raise()
    fresh, _, _, _ = make(tmp_path)
    assert fresh.state == "unlinked" and await fresh.restore() is False


async def test_code_response_without_device_code_is_an_error(tmp_path: Path) -> None:
    auth, _v, _c, _ = make(tmp_path, FakeCreds(code_response={"error": "invalid_client"}))
    with pytest.raises(NeedsLinkError) as ei:
        await auth.start_link()
    assert "invalid_client" in ei.value.message
