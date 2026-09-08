"""Tidal browse and track resolution ("browse once, play anywhere", PRD review §2.1).

The hub browses through Tidal's own API (``tidalapi``), never through the ecosystems' SMAPI
paths. Items carry the canonical Tidal id in ``content_ref``; :meth:`TidalService.tracks_for`
turns an album or playlist ref into the flat track list both adapters build queues from.

Two catalogs implement :class:`Catalog`: :class:`TidalCatalog` over a live ``tidalapi`` session
(every call runs in a worker thread; tidalapi objects never cross into the loop) and
:class:`FakeTidalCatalog`, a canned library selected with ``HUB_FAKE_TIDAL=1`` so the PWA and
Playwright can exercise browse → play → history without an account.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ..adapters.base import PlayableTrack
from ..art import ArtHelper, ArtHints
from ..content import (
    Availability,
    BrowseItem,
    BrowsePage,
    Container,
    ContentNotFoundError,
    ContentRef,
    NeedsLinkError,
)
from ..logsetup import get_logger

log = get_logger("services.tidal")

SERVICE = "tidal"
TIDAL_HEOS_SID = 10
TIDAL_SONOS_SID = 174
PAGE_MAX = 50  # Tidal caps favorites pages at 50
TRACK_PAGE = 100  # Tidal's default page for album/playlist items
CACHE_MAX_ENTRIES = 256


def _is_not_found(exc: Exception) -> bool:
    """tidalapi raises ``tidalapi.exceptions.ObjectNotFound`` (HTTP 404); we never import the
    library at module level so the fakes can raise a look-alike by class name."""
    for klass in type(exc).__mro__:
        if klass.__name__ == "ObjectNotFound":
            return True
    return False


class Catalog(Protocol):
    async def favorite_albums(self, limit: int, offset: int) -> BrowsePage: ...
    async def favorite_playlists(self, limit: int, offset: int) -> BrowsePage: ...
    async def user_playlists(self, limit: int, offset: int) -> BrowsePage: ...
    async def album(self, album_id: str) -> Container: ...
    async def playlist(self, playlist_id: str) -> Container: ...
    async def track(self, track_id: str) -> BrowseItem: ...


def _ms(seconds: Any) -> int | None:
    try:
        return int(float(seconds) * 1000) if seconds is not None else None
    except (TypeError, ValueError):
        return None


def _page(
    items: list[BrowseItem],
    limit: int,
    offset: int,
    total: int | None = None,
    *,
    server_cap: int | None = None,
) -> BrowsePage:
    """``more`` is judged against the page size the server actually honours (``min(limit,
    server_cap)``): a full page means there may be more, a short page means the end."""
    effective = min(limit, server_cap) if server_cap else limit
    more = len(items) >= effective if total is None else offset + len(items) < total
    return BrowsePage(
        items=items,
        offset=offset,
        limit=limit,
        total=total,
        next_offset=offset + len(items) if more and items else None,
    )


# --------------------------------------------------------------------------------------
# Real catalog over tidalapi
# --------------------------------------------------------------------------------------


class TidalCatalog:
    """Reads a logged-in ``tidalapi`` session. Only plain data leaves the worker thread."""

    def __init__(self, session_getter: Callable[[], Any], art: ArtHelper) -> None:
        self._session = session_getter
        self.art = art

    def _art(self, cover_id: str | None, content_id: str) -> Any:
        return self.art.ref(
            ArtHints(service=SERVICE, content_id=content_id, tidal_cover_id=cover_id or None)
        )

    def _album_item(self, album: Any) -> BrowseItem:
        artist = getattr(getattr(album, "artist", None), "name", None)
        return BrowseItem(
            content_ref=ContentRef(service=SERVICE, kind="album", id=str(album.id)),
            title=str(album.name),
            subtitle=artist,
            art=self._art(getattr(album, "cover", None), f"album:{album.id}"),
            duration_ms=_ms(getattr(album, "duration", None)),
            track_count=getattr(album, "num_tracks", None),
            artist=artist,
        )

    def _playlist_item(self, pl: Any) -> BrowseItem:
        cover = getattr(pl, "square_picture", None) or getattr(pl, "picture", None)
        creator = getattr(getattr(pl, "creator", None), "name", None)
        return BrowseItem(
            content_ref=ContentRef(service=SERVICE, kind="playlist", id=str(pl.id)),
            title=str(pl.name),
            subtitle=creator or getattr(pl, "description", None) or None,
            art=self._art(cover, f"playlist:{pl.id}"),
            duration_ms=_ms(getattr(pl, "duration", None)),
            track_count=getattr(pl, "num_tracks", None),
        )

    def _track_item(self, track: Any, index: int, parent: BrowseItem) -> BrowseItem:
        album = getattr(track, "album", None)
        artist = getattr(getattr(track, "artist", None), "name", None)
        album_id = str(album.id) if album is not None and getattr(album, "id", None) else None
        cover = getattr(album, "cover", None) if album is not None else None
        return BrowseItem(
            content_ref=ContentRef(service=SERVICE, kind="track", id=str(track.id)),
            title=str(track.name),
            subtitle=artist,
            art=self._art(cover, f"album:{album_id}") if cover else parent.art,
            duration_ms=_ms(getattr(track, "duration", None)),
            index=index,
            album_id=album_id,
            artist=artist,
            album=getattr(album, "name", None) if album is not None else None,
        )

    # -- sync workers -------------------------------------------------------------------

    @staticmethod
    def _all_tracks(fetch: Callable[..., list[Any]]) -> list[Any]:
        """Album/playlist items come back in pages of ``TRACK_PAGE``; loop until a short page."""
        out: list[Any] = []
        offset = 0
        while True:
            page = list(fetch(limit=TRACK_PAGE, offset=offset))
            out.extend(page)
            if len(page) < TRACK_PAGE:
                return out
            offset += TRACK_PAGE

    def _favorite_albums(self, limit: int, offset: int) -> BrowsePage:
        s = self._session()
        albums = s.user.favorites.albums(limit=limit, offset=offset)
        return _page([self._album_item(a) for a in albums], limit, offset, server_cap=PAGE_MAX)

    def _favorite_playlists(self, limit: int, offset: int) -> BrowsePage:
        s = self._session()
        pls = s.user.favorites.playlists(limit=limit, offset=offset)
        return _page([self._playlist_item(p) for p in pls], limit, offset, server_cap=PAGE_MAX)

    def _user_playlists(self, limit: int, offset: int) -> BrowsePage:
        s = self._session()
        pls = list(s.user.playlists())
        total = len(pls)
        return _page(
            [self._playlist_item(p) for p in pls[offset : offset + limit]], limit, offset, total
        )

    def _album(self, album_id: str) -> Container:
        s = self._session()
        ref = ContentRef(service=SERVICE, kind="album", id=album_id)
        try:
            album = s.album(album_id)
        except Exception as exc:  # noqa: BLE001 - tidalapi raises its own hierarchy
            if _is_not_found(exc):
                raise ContentNotFoundError(ref) from exc
            raise
        if album is None or getattr(album, "id", None) is None:
            raise ContentNotFoundError(ref)
        item = self._album_item(album)
        raw = self._all_tracks(album.tracks)
        tracks = [self._track_item(t, i, item) for i, t in enumerate(raw)]
        return Container(item=item, tracks=tracks)

    def _playlist(self, playlist_id: str) -> Container:
        s = self._session()
        ref = ContentRef(service=SERVICE, kind="playlist", id=playlist_id)
        try:
            pl = s.playlist(playlist_id)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise ContentNotFoundError(ref) from exc
            raise
        if pl is None or getattr(pl, "id", None) is None:
            raise ContentNotFoundError(ref)
        item = self._playlist_item(pl)
        raw = self._all_tracks(pl.tracks)
        tracks = [self._track_item(t, i, item) for i, t in enumerate(raw)]
        return Container(item=item, tracks=tracks)

    def _track(self, track_id: str) -> BrowseItem:
        s = self._session()
        ref = ContentRef(service=SERVICE, kind="track", id=track_id)
        try:
            t = s.track(track_id, with_album=True)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise ContentNotFoundError(ref) from exc
            raise
        if t is None or getattr(t, "id", None) is None:
            raise ContentNotFoundError(ref)
        parent = BrowseItem(content_ref=ref, title=str(t.name))
        return self._track_item(t, 0, parent)

    # -- async surface --------------------------------------------------------------------

    async def favorite_albums(self, limit: int, offset: int) -> BrowsePage:
        return await asyncio.to_thread(self._favorite_albums, limit, offset)

    async def favorite_playlists(self, limit: int, offset: int) -> BrowsePage:
        return await asyncio.to_thread(self._favorite_playlists, limit, offset)

    async def user_playlists(self, limit: int, offset: int) -> BrowsePage:
        return await asyncio.to_thread(self._user_playlists, limit, offset)

    async def album(self, album_id: str) -> Container:
        return await asyncio.to_thread(self._album, album_id)

    async def playlist(self, playlist_id: str) -> Container:
        return await asyncio.to_thread(self._playlist, playlist_id)

    async def track(self, track_id: str) -> BrowseItem:
        return await asyncio.to_thread(self._track, track_id)


# --------------------------------------------------------------------------------------
# Fake catalog (HUB_FAKE_TIDAL=1)
# --------------------------------------------------------------------------------------

_FAKE_ALBUMS: list[tuple[str, str, str, int, str]] = [
    # id, title, artist, tracks, art name
    ("101", "Warm Glow", "Analog Heart", 9, "warm-glow"),
    ("102", "Night Drive", "Neon Static", 11, "night-drive"),
    ("103", "Low Tide", "The Saltwater Club", 10, "low-tide"),
    ("104", "Copper", "Marlowe", 8, "copper"),
    ("105", "Signal to Noise", "Oscillate", 12, "signal-noise"),
    ("106", "Quiet Hours", "Lena Ward", 7, "quiet-hours"),
]
_FAKE_PLAYLISTS: list[tuple[str, str, str, list[str], str]] = [
    # id, title, subtitle, album ids drawn from, art name
    ("p-1", "Sunday morning", "You", ["106", "103"], "sunday"),
    ("p-2", "Dinner party", "You", ["101", "104"], "dinner"),
    ("p-3", "Late night", "You", ["102", "105"], "late-night"),
    ("p-4", "Tidal Rising", "Tidal", ["104", "102", "106"], "rising"),
]
_TRACK_WORDS = (
    "Signal",
    "Carrier",
    "Sideband",
    "Harmonic",
    "Overtone",
    "Cadence",
    "Undertow",
    "Halo",
    "Ember",
    "Static",
    "Drift",
    "Bloom",
)


class FakeTidalCatalog:
    """Deterministic canned library; ``linked`` is toggled by the dev scenarios."""

    def __init__(self, art: ArtHelper) -> None:
        self.art = art
        self.linked = True
        self.calls = 0

    def _art(self, name: str, content_id: str) -> Any:
        return self.art.ref(
            ArtHints(device_url=f"fake://art/{name}", service=SERVICE, content_id=content_id)
        )

    def _album_item(self, row: tuple[str, str, str, int, str]) -> BrowseItem:
        aid, title, artist, n, art = row
        return BrowseItem(
            content_ref=ContentRef(service=SERVICE, kind="album", id=aid),
            title=title,
            subtitle=artist,
            art=self._art(art, f"album:{aid}"),
            duration_ms=n * 214_000,
            track_count=n,
            artist=artist,
        )

    def _tracks(self, aid: str, parent: BrowseItem, base_index: int = 0) -> list[BrowseItem]:
        row = next(r for r in _FAKE_ALBUMS if r[0] == aid)
        out: list[BrowseItem] = []
        for i in range(row[3]):
            tid = f"{aid}{i + 1:02d}"
            out.append(
                BrowseItem(
                    content_ref=ContentRef(service=SERVICE, kind="track", id=tid),
                    title=f"{_TRACK_WORDS[i % len(_TRACK_WORDS)]} {i + 1}",
                    subtitle=row[2],
                    art=parent.art
                    if parent.content_ref.id == aid
                    else self._art(row[4], f"album:{aid}"),
                    duration_ms=180_000 + (i * 7_000) % 90_000,
                    index=base_index + i,
                    album_id=aid,
                    artist=row[2],
                    album=row[1],
                )
            )
        return out

    def _playlist_item(self, row: tuple[str, str, str, list[str], str]) -> BrowseItem:
        pid, title, sub, albums, art = row
        n = sum(3 for _ in albums)
        return BrowseItem(
            content_ref=ContentRef(service=SERVICE, kind="playlist", id=pid),
            title=title,
            subtitle=sub,
            art=self._art(art, f"playlist:{pid}"),
            duration_ms=n * 200_000,
            track_count=n,
        )

    def _check(self) -> None:
        self.calls += 1
        if not self.linked:
            raise NeedsLinkError(SERVICE, "Tidal is not connected. Link it in Settings.")

    async def favorite_albums(self, limit: int, offset: int) -> BrowsePage:
        self._check()
        items = [self._album_item(r) for r in _FAKE_ALBUMS]
        return _page(items[offset : offset + limit], limit, offset, len(items))

    async def favorite_playlists(self, limit: int, offset: int) -> BrowsePage:
        self._check()
        items = [self._playlist_item(r) for r in _FAKE_PLAYLISTS if r[2] != "You"]
        return _page(items[offset : offset + limit], limit, offset, len(items))

    async def user_playlists(self, limit: int, offset: int) -> BrowsePage:
        self._check()
        items = [self._playlist_item(r) for r in _FAKE_PLAYLISTS if r[2] == "You"]
        return _page(items[offset : offset + limit], limit, offset, len(items))

    async def album(self, album_id: str) -> Container:
        self._check()
        row = next((r for r in _FAKE_ALBUMS if r[0] == album_id), None)
        if row is None:
            raise ContentNotFoundError(ContentRef(service=SERVICE, kind="album", id=album_id))
        item = self._album_item(row)
        return Container(item=item, tracks=self._tracks(album_id, item))

    async def track(self, track_id: str) -> BrowseItem:
        self._check()
        for row in _FAKE_ALBUMS:
            if track_id.startswith(row[0]) and len(track_id) == len(row[0]) + 2:
                item = self._album_item(row)
                for t in self._tracks(row[0], item):
                    if t.content_ref.id == track_id:
                        return t
        raise ContentNotFoundError(ContentRef(service=SERVICE, kind="track", id=track_id))

    async def playlist(self, playlist_id: str) -> Container:
        self._check()
        row = next((r for r in _FAKE_PLAYLISTS if r[0] == playlist_id), None)
        if row is None:
            raise ContentNotFoundError(ContentRef(service=SERVICE, kind="playlist", id=playlist_id))
        item = self._playlist_item(row)
        tracks: list[BrowseItem] = []
        for aid in row[3]:
            tracks.extend(self._tracks(aid, item, base_index=len(tracks))[:3])
        for i, t in enumerate(tracks):
            t.index = i
        return Container(item=item, tracks=tracks)


# --------------------------------------------------------------------------------------
# Service: cache + availability + track resolution
# --------------------------------------------------------------------------------------


class TidalService:
    """Facade the API uses: link check, short-TTL cache, availability stamping, track lists."""

    def __init__(
        self,
        catalog: Catalog,
        *,
        is_linked: Callable[[], bool],
        availability: Callable[[], Availability],
        cache_ttl_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        before: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self.catalog = catalog
        self._is_linked = is_linked
        self._availability = availability
        self.cache_ttl_s = cache_ttl_s
        self.clock = clock
        self._before = before  # e.g. token refresh, run before every uncached catalog call
        self._cache: dict[str, tuple[float, Any]] = {}

    @property
    def linked(self) -> bool:
        return self._is_linked()

    def _require_linked(self) -> None:
        if not self._is_linked():
            raise NeedsLinkError(SERVICE, "Tidal is not connected. Link it in Settings.")

    def refresh(self) -> int:
        n = len(self._cache)
        self._cache.clear()
        return n

    async def _cached(self, key: str, loader: Callable[[], Any]) -> Any:
        hit = self._cache.get(key)
        now = self.clock()
        if hit and now - hit[0] < self.cache_ttl_s:
            return hit[1]
        if self._before is not None:
            await self._before()
        value = await loader()
        # Purge expired entries on write and cap the size (oldest first) so the cache cannot
        # grow without bound under a browse-heavy client.
        self._cache = {k: v for k, v in self._cache.items() if now - v[0] < self.cache_ttl_s}
        while len(self._cache) >= CACHE_MAX_ENTRIES:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (now, value)
        return value

    def _stamp(self, items: list[BrowseItem]) -> None:
        avail = self._availability()
        for item in items:
            item.availability = avail

    async def favorite_albums(self, limit: int = 50, offset: int = 0) -> BrowsePage:
        self._require_linked()
        limit = max(1, min(limit, PAGE_MAX))
        page: BrowsePage = await self._cached(
            f"fav-albums:{limit}:{offset}", lambda: self.catalog.favorite_albums(limit, offset)
        )
        page = page.model_copy(deep=True)
        self._stamp(page.items)
        return page

    async def favorite_playlists(self, limit: int = 50, offset: int = 0) -> BrowsePage:
        self._require_linked()
        limit = max(1, min(limit, PAGE_MAX))
        page: BrowsePage = await self._cached(
            f"fav-playlists:{limit}:{offset}",
            lambda: self.catalog.favorite_playlists(limit, offset),
        )
        page = page.model_copy(deep=True)
        self._stamp(page.items)
        return page

    async def user_playlists(self, limit: int = 50, offset: int = 0) -> BrowsePage:
        self._require_linked()
        limit = max(1, min(limit, PAGE_MAX))
        page: BrowsePage = await self._cached(
            f"user-playlists:{limit}:{offset}", lambda: self.catalog.user_playlists(limit, offset)
        )
        page = page.model_copy(deep=True)
        self._stamp(page.items)
        return page

    async def container(self, ref: ContentRef) -> Container:
        self._require_linked()
        if ref.kind == "album":
            c: Container = await self._cached(f"album:{ref.id}", lambda: self.catalog.album(ref.id))
        elif ref.kind == "playlist":
            c = await self._cached(f"playlist:{ref.id}", lambda: self.catalog.playlist(ref.id))
        else:
            raise ContentNotFoundError(ref)
        c = c.model_copy(deep=True)
        self._stamp([c.item, *c.tracks])
        return c

    async def tracks_for(self, ref: ContentRef) -> tuple[BrowseItem, list[PlayableTrack]]:
        """The flat track list an adapter needs to build a queue, plus the item for history."""
        if ref.kind == "track":
            self._require_linked()
            item: BrowseItem = await self._cached(
                f"track:{ref.id}", lambda: self.catalog.track(ref.id)
            )
            item = item.model_copy(deep=True)
            self._stamp([item])
            return item, [
                PlayableTrack(
                    service=SERVICE,
                    track_id=ref.id,
                    title=item.title,
                    artist=item.artist,
                    album=item.album,
                    album_id=item.album_id,
                    duration_ms=item.duration_ms,
                )
            ]
        c = await self.container(ref)
        playable = [
            PlayableTrack(
                service=SERVICE,
                track_id=t.content_ref.id,
                title=t.title,
                artist=t.artist,
                album=t.album,
                album_id=t.album_id,
                playlist_id=ref.id if ref.kind == "playlist" else None,
                duration_ms=t.duration_ms,
            )
            for t in c.tracks
        ]
        return c.item, playable
