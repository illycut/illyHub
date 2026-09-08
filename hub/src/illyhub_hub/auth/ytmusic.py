"""YouTube Music account linking via Google's device-code OAuth flow, persisted in the vault.

``ytmusicapi`` implements the "TV and limited input" OAuth flow: the hub asks Google for a
device code (``OAuthCredentials.get_code``), the user enters the short code at the verification
URL, and the hub polls ``token_from_code`` until Google returns a token. That flow needs a Google
Cloud OAuth client of the TV type with the YouTube Data API enabled; its id and secret come from
``HUB_YTMUSIC_CLIENT_ID`` / ``HUB_YTMUSIC_CLIENT_SECRET`` (see ``.env.example``). Tokens live
only in the vault; nothing here logs them.

Every ``ytmusicapi`` call is blocking HTTP and runs in a worker thread. Tests inject fake
credential and client factories; nothing in this module touches the network on its own.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from ..content import NeedsClientConfigError, NeedsLinkError
from ..logsetup import get_logger
from ..tasks import cancel_all, spawn
from .vault import Vault

log = get_logger("auth.ytmusic")

SERVICE = "ytmusic"
LABEL = "YouTube Music"
REFRESH_MARGIN = timedelta(minutes=5)
MISSING_CLIENT_MSG = (
    "YouTube Music linking needs a Google OAuth client: set HUB_YTMUSIC_CLIENT_ID and "
    "HUB_YTMUSIC_CLIENT_SECRET on the hub (see .env.example)."
)
EXPIRED_MSG = f"{LABEL} session expired. Link it again."
# Google's device-flow polling responses that mean "keep waiting" / "stop".
PENDING_ERRORS = {"authorization_pending", "slow_down"}
FATAL_ERRORS = {"expired_token", "access_denied", "invalid_grant"}


class CredentialsLike(Protocol):
    """The subset of ``ytmusicapi.OAuthCredentials`` the hub relies on."""

    def get_code(self) -> dict[str, Any]: ...
    def token_from_code(self, device_code: str) -> dict[str, Any]: ...
    def refresh_token(self, refresh_token: str) -> dict[str, Any]: ...


CredentialsFactory = Callable[[str, str], CredentialsLike]
# (token dict, credentials) -> a YTMusic-like client
ClientFactory = Callable[[dict[str, Any], CredentialsLike], Any]


def default_credentials_factory(  # pragma: no cover - network library
    client_id: str, client_secret: str
) -> CredentialsLike:
    from ytmusicapi import OAuthCredentials

    return OAuthCredentials(client_id=client_id, client_secret=client_secret)


def default_client_factory(  # pragma: no cover - network library
    token: dict[str, Any], creds: CredentialsLike
) -> Any:
    """``YTMusic`` given a token dict plus credentials wraps it in its own ``RefreshingToken``
    that refreshes lazily on the next request. The hub deliberately front-runs that: it refreshes
    five minutes early, persists the fresh token to the vault, and rebuilds the client, so the
    library's in-memory refresh is never the copy of record (it would not reach the vault)."""
    from ytmusicapi import YTMusic

    return YTMusic(auth=dict(token), oauth_credentials=creds)  # type: ignore[arg-type]


def _close_client(client: Any) -> None:
    """Release the old ``requests.Session`` when a client is replaced (best effort)."""
    session = getattr(client, "_session", None)
    close = getattr(session, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


@dataclass
class PendingLink:
    device_code: str
    user_code: str
    verification_url: str
    expires_at: datetime
    interval_s: float


class YTMusicAuth:
    """Owns the linked YouTube Music token (if any) and the in-progress link flow (if any).

    Mirrors :class:`~illyhub_hub.auth.tidal.TidalAuth` so the API and the app treat both
    accounts the same way (``state`` ∈ linked | pending | restoring | unlinked)."""

    def __init__(
        self,
        vault: Vault,
        *,
        client_id: str | None,
        client_secret: str | None,
        credentials_factory: CredentialsFactory = default_credentials_factory,
        client_factory: ClientFactory = default_client_factory,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.vault = vault
        self.client_id = client_id
        self.client_secret = client_secret
        self._credentials_factory = credentials_factory
        self._client_factory = client_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self.token: dict[str, Any] | None = None
        self.account_name: str | None = None
        self.pending: PendingLink | None = None
        self.last_error: str | None = None
        self._creds: CredentialsLike | None = None
        self._client: Any = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()
        self._restored = not self.has_stored_tokens()
        self.slow_down_step_s = 5.0  # Google asks for +5s per slow_down; tests shrink it

    # -- configuration ----------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _credentials(self) -> CredentialsLike:
        if not self.configured:
            raise NeedsClientConfigError(SERVICE, MISSING_CLIENT_MSG)
        if self._creds is None:
            assert self.client_id and self.client_secret
            self._creds = self._credentials_factory(self.client_id, self.client_secret)
        return self._creds

    def has_stored_tokens(self) -> bool:
        stored = self.vault.get(SERVICE)
        return bool(stored and stored.get("token", {}).get("access_token"))

    # -- lifecycle ----------------------------------------------------------------------------

    async def restore(self) -> bool:
        """Load the token from the vault and validate it (network, in a thread)."""
        try:
            stored = self.vault.get(SERVICE)
            token = (stored or {}).get("token")
            if not token or not token.get("access_token"):
                return False
            if not self.configured:
                self.last_error = MISSING_CLIENT_MSG
                return False
            try:
                client = await asyncio.to_thread(self._client_factory, token, self._credentials())
                name = await asyncio.to_thread(_account_name, client)
            except Exception as exc:  # noqa: BLE001 - network library
                self.last_error = f"{LABEL} session restore failed: {exc.__class__.__name__}"
                log.warning("ytmusic restore failed", extra={"extra": {"error": str(exc)}})
                return False
            if self.token is not None:
                return True  # a link completed while we were validating; keep it
            self.token = dict(token)
            self._client = client
            self.account_name = (stored or {}).get("account_name") or name
            self._persist()
            self.last_error = None
            return True
        finally:
            self._restored = True

    @property
    def restoring(self) -> bool:
        return not self._restored and self.token is None

    @property
    def linked(self) -> bool:
        return self.token is not None

    @property
    def state(self) -> str:
        if self.token is not None:
            return "linked"
        if self.pending is not None and self.pending.expires_at > self.clock():
            return "pending"
        if self.restoring:
            return "restoring"
        return "unlinked"

    @property
    def expires_at(self) -> datetime | None:
        if self.token is None:
            return None
        at = self.token.get("expires_at")
        if at is None:
            return None
        return datetime.fromtimestamp(int(at), tz=UTC)

    async def aclose(self) -> None:
        await cancel_all(self._tasks)

    def status(self) -> dict[str, Any]:
        pending = self.pending
        if pending is not None and pending.expires_at <= self.clock():
            self.pending = pending = None
            if self.last_error is None:
                self.last_error = f"{LABEL} link code expired; start again."
        last_error = self.last_error
        last_error_code: str | None = None
        if not self.configured and not self.linked:
            last_error = last_error or MISSING_CLIENT_MSG
            last_error_code = "needs_client_config"
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
            "last_error": last_error,
            "last_error_code": last_error_code,
        }

    def client_or_raise(self) -> Any:
        if self.token is None or self._client is None:
            raise NeedsLinkError(SERVICE, f"{LABEL} is not connected. Link it in Settings.")
        return self._client

    # -- link flow ----------------------------------------------------------------------------

    async def start_link(self) -> dict[str, Any]:
        """Ask Google for a device code; approval is polled in the background. A previous
        unfinished flow is cancelled first so only one poller is ever live."""
        async with self._lock:
            return await self._start_link_locked()

    async def _start_link_locked(self) -> dict[str, Any]:
        creds = self._credentials()
        await cancel_all(self._tasks)
        self.pending = None
        code = await asyncio.to_thread(creds.get_code)
        if "device_code" not in code:
            self.last_error = f"{LABEL} did not issue a link code ({code.get('error', 'unknown')})."
            raise NeedsLinkError(SERVICE, self.last_error)
        expires_in = float(code.get("expires_in", 1800))
        interval = float(code.get("interval", 5))
        pending = PendingLink(
            device_code=str(code["device_code"]),
            user_code=str(code.get("user_code", "")),
            verification_url=str(code.get("verification_url") or "https://www.google.com/device"),
            expires_at=self.clock() + timedelta(seconds=expires_in),
            interval_s=interval,
        )
        self.pending = pending
        self.last_error = None
        spawn(self._await_approval(creds, pending), self._tasks)
        log.info("ytmusic link started", extra={"extra": {"expires_in_s": expires_in}})
        return {
            "service": SERVICE,
            "user_code": pending.user_code,
            "verification_url": pending.verification_url,
            "expires_in_s": int(expires_in),
            "interval_s": interval,
        }

    async def _await_approval(self, creds: CredentialsLike, pending: PendingLink) -> None:
        interval = pending.interval_s
        while self.pending is pending:
            if pending.expires_at <= self.clock():
                self.last_error = f"{LABEL} link code expired; start again."
                if self.pending is pending:
                    self.pending = None
                return
            await asyncio.sleep(interval)
            if self.pending is not pending:
                return
            try:
                resp = await asyncio.to_thread(creds.token_from_code, pending.device_code)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - network library
                self.last_error = f"{LABEL} link did not complete: {exc.__class__.__name__}"
                log.warning("ytmusic link failed", extra={"extra": {"error": str(exc)}})
                if self.pending is pending:
                    self.pending = None
                return
            if self.pending is not pending:
                # unlink() or a newer start_link() superseded this flow: never adopt the token.
                log.info("ytmusic approval arrived for a superseded link flow; ignored")
                return
            if "access_token" in resp:
                await self._adopt(resp, pending)
                return
            error = str(resp.get("error", ""))
            if error == "slow_down":
                interval += self.slow_down_step_s
                continue
            if error in PENDING_ERRORS or not error:
                continue
            self.last_error = f"{LABEL} link did not complete ({error})."
            log.warning("ytmusic link refused", extra={"extra": {"error": error}})
            self.pending = None
            return

    async def _adopt(self, resp: dict[str, Any], pending: PendingLink) -> None:
        token = _normalize_token(resp)
        try:
            client = await asyncio.to_thread(self._client_factory, token, self._credentials())
            name = await asyncio.to_thread(_account_name, client)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{LABEL} linked but the client failed: {exc.__class__.__name__}"
            log.warning("ytmusic client failed after link", extra={"extra": {"error": str(exc)}})
            self.pending = None
            return
        if self.pending is not pending:
            return
        self.pending = None
        self.token = token
        self._client = client
        self.account_name = name
        self.last_error = None
        self._persist()
        log.info("ytmusic linked", extra={"extra": {"account": self.account_name}})

    async def unlink(self) -> None:
        async with self._lock:
            self.pending = None  # any in-flight poller sees the swap and drops its result
            await cancel_all(self._tasks)
            self.token = None
            self._client = None
            self.account_name = None
            self.vault.delete(SERVICE)
            self._restored = True
            log.info("ytmusic unlinked")

    # -- tokens -------------------------------------------------------------------------------

    def _persist(self) -> None:
        if self.token is None:
            return
        self.vault.put(SERVICE, {"token": dict(self.token), "account_name": self.account_name})

    async def ensure_fresh(self) -> Any:
        """Refresh the access token when it is within five minutes of expiry; returns the client."""
        client = self.client_or_raise()
        exp = self.expires_at
        if exp is None or exp - self.clock() > REFRESH_MARGIN:
            return client
        async with self._lock:
            if self._client is not client:
                return self.client_or_raise()
            exp = self.expires_at
            if exp is not None and exp - self.clock() > REFRESH_MARGIN:
                return client  # another caller refreshed while we waited for the lock
            assert self.token is not None
            refresh = self.token.get("refresh_token")
            if not refresh:
                raise NeedsLinkError(SERVICE, EXPIRED_MSG)
            try:
                fresh = await asyncio.to_thread(self._credentials().refresh_token, str(refresh))
                ok = "access_token" in fresh
            except Exception as exc:  # noqa: BLE001
                log.warning("ytmusic refresh failed", extra={"extra": {"error": str(exc)}})
                ok, fresh = False, {}
            if not ok:
                # Drop the dead token everywhere so a restart cannot re-adopt it.
                old = self._client
                self.token = None
                self._client = None
                self.account_name = None
                self.vault.delete(SERVICE)
                self._restored = True
                self.last_error = EXPIRED_MSG
                await asyncio.to_thread(_close_client, old)
                raise NeedsLinkError(SERVICE, EXPIRED_MSG)
            token = dict(self.token)
            token["access_token"] = fresh["access_token"]
            token["expires_in"] = int(fresh.get("expires_in", 3600))
            token["expires_at"] = int(time.time()) + token["expires_in"]
            self.token = token
            old = self._client
            try:
                self._client = await asyncio.to_thread(
                    self._client_factory, token, self._credentials()
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("ytmusic client rebuild failed", extra={"extra": {"error": str(exc)}})
                raise NeedsLinkError(SERVICE, f"{LABEL} is unavailable right now.") from exc
            await asyncio.to_thread(_close_client, old)
            self._persist()
            return self._client


def _normalize_token(resp: dict[str, Any]) -> dict[str, Any]:
    """Shape ``ytmusicapi`` expects for ``YTMusic(auth=...)``: a RefreshableTokenDict with an
    absolute ``expires_at`` so the library and the hub agree on expiry."""
    token = {
        "scope": resp.get("scope", "https://www.googleapis.com/auth/youtube"),
        "token_type": resp.get("token_type", "Bearer"),
        "access_token": resp["access_token"],
        "refresh_token": resp.get("refresh_token", ""),
        "expires_in": int(resp.get("expires_in", 3600)),
    }
    token["expires_at"] = int(resp.get("expires_at") or (time.time() + token["expires_in"]))
    return token


def _account_name(client: Any) -> str | None:
    getter = getattr(client, "get_account_info", None)
    if getter is None:
        return None
    try:
        info = getter() or {}
    except Exception:  # noqa: BLE001 - optional nicety
        return None
    for key in ("accountName", "channelHandle", "name"):
        value = info.get(key) if isinstance(info, dict) else None
        if value:
            return str(value)
    return None
