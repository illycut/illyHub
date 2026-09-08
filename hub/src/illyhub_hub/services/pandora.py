"""Pandora stations: browsed **per ecosystem**, merged by name (Phase 5, PRD §3.5).

Pandora has no hub-side account here. Each vendor app is linked to Pandora on its own, and each
ecosystem lists the user's stations through its native path (HEOS ``browse/browse`` on source 1,
Sonos SMAPI via SoCo). The hub asks the adapters (:meth:`PlaybackAdapter.list_stations`), unions
the two lists by a normalised station name, and hands the client one vendor-neutral
``content_ref`` per station: ``{service: "pandora", kind: "station", id: <key>}``. Playing a
station on a side uses that side's own ids, which the service keeps in a per-vendor map.

Cross-system *sync* of a station is impossible (each device session gets its own track sequence),
so Sync Play refuses stations; playing the same station on both sides at once may trip Pandora's
one-stream-per-account rule, which the router surfaces as a ``pandora_concurrent`` warning.

Per-vendor outcomes are kept apart: *not linked* (no account on that side), *auth fault* (linked
but the vendor's service session is broken; re-sign-in fixes it), *empty* (linked, no stations),
and *error* (browse failed; retried next call, never cached). Only the first three are cached.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any

from ..adapters.base import (
    ContentUnavailableError,
    PlaybackAdapter,
    ServiceAuthError,
    VendorStation,
)
from ..art import ArtHelper, ArtHints
from ..content import Availability, BrowseItem, ContentNotFoundError, ContentRef, NeedsLinkError
from ..logsetup import get_logger

log = get_logger("services.pandora")

SERVICE = "pandora"
VENDORS = ("heos", "sonos")
PAGE_MAX = 500  # the merged list is served whole; this is the BrowsePage.limit it reports
KEY_MAX = 180  # ContentRef.id allows 200; leave room for a "-NN" collision suffix
_KEY_RE = re.compile(r"[^a-z0-9]+")
NOT_LINKED_MSG = "Pandora is not linked in the HEOS or Sonos app."


def station_key(name: str) -> str:
    """Vendor-neutral id for a station name.

    NFKD-fold and drop combining marks ("Café" → "cafe", so both vendors' spellings merge),
    casefold, collapse runs of anything outside ``[a-z0-9]`` to ``-``, cap the length. Names
    with no ASCII alphanumerics at all (CJK, emoji) fall back to a hash of the original so two
    different such names never merge into one key."""
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).casefold()
    key = _KEY_RE.sub("-", folded).strip("-")[:KEY_MAX].strip("-")
    if not key:
        return "station-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return key


VendorState = str  # "ok" | "unlinked" | "auth" | "absent" | "error"


class _VendorResult:
    """One vendor's outcome. ``stations`` is None unless ``state == "ok"``; ``auth_error``
    carries the distinct message for ``"auth"``. ``"absent"`` (no adapter / not connected) and
    ``"error"`` (browse failed) are the two states the client sees as *unknown* (``null``)."""

    __slots__ = ("at", "auth_error", "state", "stations")

    def __init__(
        self,
        at: float,
        stations: list[VendorStation] | None,
        *,
        state: VendorState,
        auth_error: str | None = None,
    ) -> None:
        self.at = at
        self.stations = stations
        self.state = state
        self.auth_error = auth_error

    @property
    def linked(self) -> bool | None:
        if self.state == "ok":
            return True
        if self.state in ("unlinked", "auth"):
            return False
        return None


class PandoraService:
    """Merged station list with a short TTL cache per vendor and a per-vendor ref map.

    ``sources`` returns the live vendor → adapter map (the HEOS adapter can arrive late via
    discovery, so it is a callable, not a snapshot). ``is_connected`` gates a vendor on its
    connection state so a disconnected ecosystem reads as "not available" rather than erroring.
    """

    def __init__(
        self,
        sources: Callable[[], Mapping[str, PlaybackAdapter]],
        *,
        art: ArtHelper,
        is_connected: Callable[[str], bool] | None = None,
        cache_ttl_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sources = sources
        self.art = art
        self._is_connected = is_connected or (lambda vendor: True)
        self.cache_ttl_s = cache_ttl_s
        self.clock = clock
        self._cache: dict[str, _VendorResult] = {}
        self._inflight: dict[str, asyncio.Future[_VendorResult]] = {}  # single-flight per vendor
        self._lock = asyncio.Lock()
        self._refs: dict[str, dict[str, VendorStation]] = {}  # key -> vendor -> station
        self._names: dict[str, str] = {}  # key -> display name
        self._subtitles: dict[str, str] = {}  # key -> first vendor-supplied subtitle
        self._errors: dict[str, str] = {}  # vendor -> last browse error (not cached)
        self._last: dict[str, _VendorResult] = {}  # vendor -> most recent outcome, errors too

    # -- fetching ----------------------------------------------------------------------

    def refresh(self) -> int:
        """Drop everything: caches, the merged map, remembered errors. Returns the number of
        vendor cache entries dropped (the app calls this after link/unlink)."""
        n = len(self._cache)
        self._cache.clear()
        self._last.clear()
        self._errors.clear()
        self._refs, self._names, self._subtitles = {}, {}, {}
        return n

    @property
    def linked(self) -> dict[str, bool | None]:
        """Per-vendor truth for the client (``true`` browse succeeded, ``false`` not linked or
        auth fault, ``null`` adapter absent / browse errored / not asked yet)."""
        return {v: (r.linked if (r := self._last.get(v)) is not None else None) for v in VENDORS}

    def _fresh(self, vendor: str) -> _VendorResult | None:
        hit = self._cache.get(vendor)
        if hit and self.clock() - hit.at < self.cache_ttl_s:
            return hit
        return None

    async def _vendor(self, vendor: str) -> _VendorResult:
        """This vendor's stations, cached; concurrent callers share one fetch."""
        hit = self._fresh(vendor)
        if hit is not None:
            return hit
        async with self._lock:
            hit = self._fresh(vendor)
            if hit is not None:
                return hit
            fut = self._inflight.get(vendor)
            if fut is None:
                fut = asyncio.get_running_loop().create_future()
                self._inflight[vendor] = fut
                owner = True
            else:
                owner = False
        if not owner:
            return await fut
        try:
            result = await self._fetch(vendor)
            if result.state != "error":
                self._cache[vendor] = result  # errors are retried next call, never cached
            self._last[vendor] = result
            fut.set_result(result)
            return result
        except BaseException as exc:
            fut.set_exception(exc)
            raise
        finally:
            async with self._lock:
                self._inflight.pop(vendor, None)

    async def _fetch(self, vendor: str) -> _VendorResult:
        """Ask the adapter and classify the outcome (see :class:`_VendorResult`)."""
        now = self.clock()
        adapter = self._sources().get(vendor)
        if adapter is None or not self._is_connected(vendor):
            self._errors.pop(vendor, None)
            return _VendorResult(now, None, state="absent")
        try:
            stations = list(await adapter.list_stations(SERVICE))
        except ServiceAuthError as exc:
            self._errors.pop(vendor, None)
            log.warning(
                "station auth fault", extra={"extra": {"vendor": vendor, "error": str(exc)}}
            )
            return _VendorResult(now, None, state="auth", auth_error=str(exc))
        except ContentUnavailableError:
            self._errors.pop(vendor, None)
            return _VendorResult(now, None, state="unlinked")
        except Exception as exc:  # noqa: BLE001 - one vendor's failure must not sink the list
            self._errors[vendor] = f"{exc.__class__.__name__}: {exc}"
            log.warning(
                "station browse failed",
                extra={"extra": {"vendor": vendor, "error": self._errors[vendor]}},
            )
            return _VendorResult(now, None, state="error")
        self._errors.pop(vendor, None)
        # Deterministic keying regardless of the order the vendor returned them.
        stations.sort(key=lambda st: (st.name.casefold(), sorted(st.ids.items())))
        return _VendorResult(now, stations, state="ok")

    def _rebuild(self, per_vendor: Mapping[str, _VendorResult]) -> None:
        refs: dict[str, dict[str, VendorStation]] = {}
        names: dict[str, str] = {}
        subtitles: dict[str, str] = {}
        for vendor in VENDORS:
            for st in per_vendor[vendor].stations or []:
                key = station_key(st.name)
                # Two different stations of one vendor that normalise alike get a suffix.
                base, n = key, 2
                while key in refs and vendor in refs[key] and refs[key][vendor].ids != st.ids:
                    key = f"{base}-{n}"
                    n += 1
                refs.setdefault(key, {})[vendor] = st
                names.setdefault(key, st.name)
                if st.subtitle:
                    subtitles.setdefault(key, st.subtitle)
        self._refs, self._names, self._subtitles = refs, names, subtitles

    async def stations(self) -> list[BrowseItem]:
        """The merged list (PAN-3). Raises ``needs_link`` when no ecosystem can list stations;
        the message names an auth fault when that is the reason."""
        results = await asyncio.gather(*(self._vendor(v) for v in VENDORS))
        per_vendor = dict(zip(VENDORS, results, strict=True))
        if all(r.stations is None for r in results):
            auth = [r.auth_error for r in results if r.auth_error]
            if auth:
                raise NeedsLinkError(SERVICE, " ".join(auth))
            if self._errors:
                detail = "; ".join(f"{v}: {e}" for v, e in self._errors.items())
                raise RuntimeError(f"Pandora station browse failed ({detail})")
            raise NeedsLinkError(SERVICE, NOT_LINKED_MSG)
        self._rebuild(per_vendor)
        items = [self._item(key) for key in self._refs]
        items.sort(key=lambda it: it.title.casefold())
        return items

    def _item(self, key: str) -> BrowseItem:
        by_vendor = self._refs[key]
        first = next(iter(by_vendor.values()))
        art_url = next((st.art_url for st in by_vendor.values() if st.art_url), None)
        return BrowseItem(
            content_ref=ContentRef(service=SERVICE, kind="station", id=key),
            title=self._names.get(key, first.name),
            subtitle=self._subtitles.get(key) or "Pandora station",
            art=self.art.ref(
                ArtHints(service=SERVICE, content_id=f"station:{key}", device_url=art_url)
            ),
            availability=Availability(
                heos="heos" in by_vendor,
                sonos="sonos" in by_vendor,
            ),
        )

    # -- lookups ------------------------------------------------------------------------

    def _map_is_current(self) -> bool:
        return all(self._fresh(v) is not None for v in VENDORS) and bool(self._refs)

    async def station(self, ref: ContentRef) -> BrowseItem:
        """One station by hub key. Refetches whenever a vendor cache is cold (a station that
        appeared after the last browse must be playable), not only on a key miss."""
        if ref.service != SERVICE or ref.kind != "station":
            raise ContentNotFoundError(ref)
        if not self._map_is_current() or ref.id not in self._refs:
            await self.stations()  # raises needs_link when nothing is linked
        if ref.id not in self._refs:
            raise ContentNotFoundError(ref)
        return self._item(ref.id)

    def vendor_stations(self, ref: ContentRef) -> dict[str, VendorStation]:
        """This station's per-vendor refs (``{}`` when unknown; call :meth:`station` first)."""
        return dict(self._refs.get(ref.id, {}))

    @property
    def auth_faults(self) -> dict[str, str]:
        """vendor -> message for vendors whose Pandora session is broken (from the last fetch)."""
        return {
            v: r.auth_error
            for v in VENDORS
            if (r := self._last.get(v)) is not None and r.state == "auth" and r.auth_error
        }

    @property
    def last_error(self) -> str | None:
        """What the settings screen shows on the Pandora row: an auth fault on either side wins,
        then the latest browse error."""
        auth = [
            f"{v.title()}: {r.auth_error}"
            for v in VENDORS
            if (r := self._cache.get(v)) is not None and r.auth_error
        ]
        if auth:
            return " ".join(auth)
        if self._errors:
            return "; ".join(f"{v}: {e}" for v, e in self._errors.items())
        return None

    def debug(self) -> dict[str, Any]:
        return {"stations": len(self._refs), "errors": dict(self._errors)}
