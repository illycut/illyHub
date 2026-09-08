"""Tidal account linking via the device-code (link) flow, persisted in the vault.

``tidalapi.Session.login_oauth()`` returns the code the user types at link.tidal.com plus a
``concurrent.futures.Future`` that polls Tidal until the user approves or the code expires. The
hub wraps that future so the API can start a link and report its status without blocking.
Tokens live only in the vault; nothing here logs them.

All tidalapi calls are blocking HTTP and run in a worker thread. Tests inject a fake session
factory; nothing in this module touches the network on its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from ..content import NeedsLinkError
from ..logsetup import get_logger
from ..tasks import cancel_all, spawn
from .vault import Vault

log = get_logger("auth.tidal")

SERVICE = "tidal"
REFRESH_MARGIN = timedelta(minutes=5)


class LinkLoginLike(Protocol):
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: float
    interval: float


class TidalSessionLike(Protocol):
    """The subset of ``tidalapi.Session`` the hub relies on."""

    token_type: str | None
    access_token: str | None
    refresh_token: str | None
    expiry_time: datetime | None
    user: Any

    def login_oauth(self) -> tuple[LinkLoginLike, Any]: ...
    def load_oauth_session(
        self,
        token_type: str,
        access_token: str,
        refresh_token: str | None = None,
        expiry_time: datetime | None = None,
    ) -> bool: ...
    def check_login(self) -> bool: ...
    def token_refresh(self, refresh_token: str) -> bool: ...


SessionFactory = Callable[[], TidalSessionLike]


def default_session_factory() -> TidalSessionLike:  # pragma: no cover - network library
    import tidalapi

    return tidalapi.Session()  # type: ignore[return-value]


@dataclass
class PendingLink:
    user_code: str
    verification_url: str
    expires_at: datetime
    interval_s: float


def _account_name(session: TidalSessionLike) -> str | None:
    user = getattr(session, "user", None)
    if user is None:
        return None
    for attr in ("username", "email", "first_name"):
        value = getattr(user, attr, None)
        if value:
            return str(value)
    return None


class TidalAuth:
    """Owns the linked Tidal session (if any) and the in-progress link flow (if any)."""

    def __init__(
        self,
        vault: Vault,
        *,
        factory: SessionFactory = default_session_factory,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.vault = vault
        self.factory = factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self.session: TidalSessionLike | None = None
        self.account_name: str | None = None
        self.pending: PendingLink | None = None
        self.last_error: str | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()
        self._restored = not self.has_stored_tokens()  # nothing to restore → not "restoring"

    def has_stored_tokens(self) -> bool:
        stored = self.vault.get(SERVICE)
        return bool(stored and stored.get("access_token"))

    # -- lifecycle ----------------------------------------------------------------------

    async def restore(self) -> bool:
        """Load tokens from the vault and validate them (network, in a thread)."""
        try:
            stored = self.vault.get(SERVICE)
            if not stored or not stored.get("access_token"):
                return False
            session = self.factory()
            expiry = stored.get("expiry_time")
            try:
                ok = await asyncio.to_thread(
                    session.load_oauth_session,
                    stored.get("token_type") or "Bearer",
                    stored["access_token"],
                    stored.get("refresh_token"),
                    datetime.fromisoformat(expiry) if expiry else None,
                )
            except Exception as exc:  # noqa: BLE001 - network library
                self.last_error = f"Tidal session restore failed: {exc.__class__.__name__}"
                log.warning("tidal restore failed", extra={"extra": {"error": str(exc)}})
                return False
            if not ok:
                self.last_error = "Stored Tidal tokens were rejected; relink."
                return False
            if self.session is not None:
                return True  # a link completed while we were validating; keep it
            self.session = session
            self.account_name = stored.get("account_name") or _account_name(session)
            self._persist()
            self.last_error = None
            return True
        finally:
            self._restored = True

    @property
    def restoring(self) -> bool:
        return not self._restored and self.session is None

    @property
    def state(self) -> str:
        """``linked | pending | restoring | unlinked`` — what the client should render."""
        if self.session is not None:
            return "linked"
        if self.pending is not None and self.pending.expires_at > self.clock():
            return "pending"
        if self.restoring:
            return "restoring"
        return "unlinked"

    async def aclose(self) -> None:
        await cancel_all(self._tasks)

    # -- properties -----------------------------------------------------------------------

    @property
    def linked(self) -> bool:
        return self.session is not None

    @property
    def expires_at(self) -> datetime | None:
        if self.session is None:
            return None
        exp = getattr(self.session, "expiry_time", None)
        if isinstance(exp, datetime) and exp.tzinfo is None:
            return exp.replace(tzinfo=UTC)
        return exp

    def status(self) -> dict[str, Any]:
        pending = self.pending
        if pending is not None and pending.expires_at <= self.clock():
            self.pending = pending = None
        return {
            "service": SERVICE,
            "linked": self.linked,
            "state": self.state,
            "account_name": self.account_name if self.linked else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "pending": (
                {
                    "user_code": pending.user_code,
                    "verification_url": pending.verification_url,
                    "expires_at": pending.expires_at.isoformat(),
                }
                if pending
                else None
            ),
            "last_error": self.last_error,
        }

    def session_or_raise(self) -> TidalSessionLike:
        if self.session is None:
            raise NeedsLinkError(SERVICE, "Tidal is not connected. Link it in Settings.")
        return self.session

    # -- link flow ------------------------------------------------------------------------

    async def start_link(self) -> dict[str, Any]:
        """Begin the device-code flow; the approval is awaited in the background. A previous
        unfinished flow is cancelled first so only one poller is ever live."""
        await cancel_all(self._tasks)
        self.pending = None
        session = self.factory()
        link, future = await asyncio.to_thread(session.login_oauth)
        expires_at = self.clock() + timedelta(seconds=float(link.expires_in))
        self.pending = PendingLink(
            user_code=link.user_code,
            verification_url=link.verification_uri_complete or link.verification_uri,
            expires_at=expires_at,
            interval_s=float(link.interval),
        )
        self.last_error = None
        spawn(self._await_approval(session, future, self.pending), self._tasks)
        log.info("tidal link started", extra={"extra": {"expires_in_s": link.expires_in}})
        return {
            "service": SERVICE,
            "user_code": link.user_code,
            "verification_url": self.pending.verification_url,
            "expires_in_s": int(link.expires_in),
            "interval_s": link.interval,
        }

    async def _await_approval(
        self, session: TidalSessionLike, future: Any, pending: PendingLink
    ) -> None:
        try:
            if asyncio.isfuture(future) or asyncio.iscoroutine(future):
                await future
            else:
                await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - tidalapi raises TimeoutError on expiry
            self.last_error = f"Tidal link did not complete: {exc.__class__.__name__}"
            log.warning("tidal link failed", extra={"extra": {"error": str(exc)}})
            if self.pending is pending:
                self.pending = None
            return
        if self.pending is not pending:
            # unlink() or a newer start_link() superseded this flow: never adopt the session.
            log.info("tidal approval arrived for a superseded link flow; ignored")
            return
        self.pending = None
        self.session = session
        self.account_name = _account_name(session)
        self.last_error = None
        self._persist()
        log.info("tidal linked", extra={"extra": {"account": self.account_name}})

    async def unlink(self) -> None:
        async with self._lock:
            self.pending = None  # any in-flight poller sees the swap and drops its result
            await cancel_all(self._tasks)
            self.session = None
            self.account_name = None
            self.vault.delete(SERVICE)
            self._restored = True
            log.info("tidal unlinked")

    # -- tokens -----------------------------------------------------------------------------

    def _persist(self) -> None:
        s = self.session
        if s is None:
            return
        exp = getattr(s, "expiry_time", None)
        self.vault.put(
            SERVICE,
            {
                "token_type": getattr(s, "token_type", None) or "Bearer",
                "access_token": getattr(s, "access_token", None),
                "refresh_token": getattr(s, "refresh_token", None),
                "expiry_time": exp.isoformat() if isinstance(exp, datetime) else None,
                "account_name": self.account_name,
            },
        )

    async def ensure_fresh(self) -> TidalSessionLike:
        """Refresh the access token when it is within five minutes of expiry."""
        session = self.session_or_raise()
        exp = self.expires_at
        if exp is None or exp - self.clock() > REFRESH_MARGIN:
            return session
        async with self._lock:
            if self.session is not session:
                return self.session_or_raise()
            exp = self.expires_at
            if exp is not None and exp - self.clock() > REFRESH_MARGIN:
                return session  # another caller refreshed while we waited for the lock
            refresh = getattr(session, "refresh_token", None)
            if not refresh:
                raise NeedsLinkError(SERVICE, "Tidal session expired. Link it again in Settings.")
            try:
                ok = await asyncio.to_thread(session.token_refresh, refresh)
            except Exception as exc:  # noqa: BLE001
                log.warning("tidal refresh failed", extra={"extra": {"error": str(exc)}})
                ok = False
            if not ok:
                self.session = None
                self.last_error = "Tidal token refresh failed; relink."
                raise NeedsLinkError(SERVICE, "Tidal session expired. Link it again in Settings.")
            self._persist()
            return session
