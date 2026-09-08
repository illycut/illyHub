"""YouTube Music browse through ``ytmusicapi`` ("browse once, play anywhere", PRD review §2.1).

The hub reads the user's library with the service's own API and hands canonical ids to the
adapters: ``content_ref`` ids are the YouTube ``videoId`` (tracks), the album ``browseId``
(albums) and the ``playlistId`` (playlists). Sonos has native YouTube Music, so the Sonos adapter
builds queue entries from those ids (docs/spikes/ytmusic-sonos.md). HEOS has no YouTube Music
source; the HEOS side stays unavailable unless the yt-dlp spike (docs/spikes/ytmusic-heos.md)
becomes a go.

:class:`YTMusicCatalog` implements the same :class:`~illyhub_hub.services.tidal.Catalog` protocol
as Tidal, so :class:`~illyhub_hub.services.tidal.TidalService` fronts it unchanged (``service=
"ytmusic"``). ``favorite_playlists`` is always empty: ytmusicapi does not separate saved from
owned playlists, so every library playlist is served as a user playlist. Library albums are
served as ``favorite_albums`` and fold into the home "Favorite albums" grid with the YouTube
Music badge (PRD §3.4a lists no separate albums section).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..art import ArtHelper, ArtHints, Thumbnail
from ..content import (
    BrowseItem,
    BrowsePage,
    Container,
    ContentNotFoundError,
    ContentRef,
    NeedsLinkError,
)
from ..logsetup import get_logger
from .tidal import _page

log = get_logger("services.ytmusic")

SERVICE = "ytmusic"
LABEL = "YouTube Music"
PAGE_MAX = 100  # the hub pages client-side over the library it fetched
DEFAULT_MAX_TRACKS = 500  # HUB_YTMUSIC_MAX_TRACKS
DEFAULT_MAX_LIBRARY_ITEMS = 1000  # HUB_YTMUSIC_MAX_LIBRARY_ITEMS


def _thumbs(raw: Any) -> list[Thumbnail]:
    out: list[Thumbnail] = []
    for t in raw or []:
        if isinstance(t, dict) and t.get("url"):
            out.append(
                Thumbnail(
                    url=str(t["url"]),
                    width=int(t.get("width") or 0),
                    height=int(t.get("height") or 0),
                )
            )
    return out


def _artists(raw: Any) -> str | None:
    if isinstance(raw, dict):
        raw = [raw]
    names = [str(a.get("name")) for a in raw or [] if isinstance(a, dict) and a.get("name")]
    return ", ".join(names) or None


def _ms(seconds: Any, fallback: Any = None) -> int | None:
    if seconds is not None:
        try:
            return int(float(seconds) * 1000)
        except (TypeError, ValueError):
            pass
    if isinstance(fallback, str) and ":" in fallback:
        parts = fallback.split(":")
        try:
            total = 0
            for p in parts:
                total = total * 60 + int(p)
            return total * 1000
        except ValueError:
            return None
    return None


class YTMusicCatalog:
    """Reads a linked ``ytmusicapi.YTMusic`` client. Only plain data leaves the worker thread."""

    def __init__(
        self,
        client_getter: Callable[[], Any],
        art: ArtHelper,
        *,
        max_tracks: int = DEFAULT_MAX_TRACKS,
        max_library_items: int = DEFAULT_MAX_LIBRARY_ITEMS,
    ) -> None:
        self._client = client_getter
        self.art = art
        self.max_tracks = max(1, max_tracks)
        self.max_library_items = max(1, max_library_items)

    def _art(self, thumbs: Any, content_id: str) -> Any:
        return self.art.ref(
            ArtHints(service=SERVICE, content_id=content_id, thumbnails=_thumbs(thumbs) or None)
        )

    # -- item shapes ----------------------------------------------------------------------

    def _album_item(self, a: dict[str, Any]) -> BrowseItem:
        aid = str(a.get("browseId") or a.get("id") or "")
        artist = _artists(a.get("artists"))
        return BrowseItem(
            content_ref=ContentRef(service=SERVICE, kind="album", id=aid),
            title=str(a.get("title") or ""),
            subtitle=artist or (str(a["year"]) if a.get("year") else None),
            art=self._art(a.get("thumbnails"), f"album:{aid}"),
            duration_ms=_ms(a.get("duration_seconds"), a.get("duration")),
            track_count=a.get("trackCount"),
            artist=artist,
        )

    def _playlist_item(self, p: dict[str, Any]) -> BrowseItem:
        pid = str(p.get("playlistId") or p.get("id") or "")
        author = p.get("author")
        if isinstance(author, list):
            author = _artists(author)
        elif isinstance(author, dict):
            author = author.get("name")
        count = p.get("count") or p.get("trackCount")
        try:
            track_count = int(str(count).replace(",", "")) if count is not None else None
        except ValueError:
            track_count = None
        return BrowseItem(
            content_ref=ContentRef(service=SERVICE, kind="playlist", id=pid),
            title=str(p.get("title") or ""),
            subtitle=str(author) if author else (p.get("description") or None),
            art=self._art(p.get("thumbnails"), f"playlist:{pid}"),
            duration_ms=_ms(p.get("duration_seconds"), p.get("duration")),
            track_count=track_count,
        )

    def _track_item(self, t: dict[str, Any], index: int, parent: BrowseItem) -> BrowseItem:
        vid = str(t.get("videoId") or "")
        raw_album = t.get("album")
        album = raw_album if isinstance(raw_album, dict) else None
        album_name_str = raw_album if isinstance(raw_album, str) else None
        album_id = str(album.get("id")) if album and album.get("id") else None
        if album_id is None and parent.content_ref.kind == "album":
            album_id = parent.content_ref.id
        artist = _artists(t.get("artists"))
        thumbs = t.get("thumbnails")
        return BrowseItem(
            content_ref=ContentRef(service=SERVICE, kind="track", id=vid),
            title=str(t.get("title") or ""),
            subtitle=artist,
            art=self._art(thumbs, f"track:{vid}") if thumbs else parent.art,
            duration_ms=_ms(t.get("duration_seconds"), t.get("duration")),
            index=index,
            album_id=album_id,
            artist=artist,
            album=(album.get("name") if album else None)
            or album_name_str
            or (parent.title if parent.content_ref.kind == "album" else None),
        )

    # -- sync workers -------------------------------------------------------------------

    def _search(self, query: str, limit: int) -> dict[str, list[BrowseItem]]:
        """``YTMusic.search(query, filter=albums|playlists|songs)``, one call per group."""
        c = self._client()
        albums = [
            self._album_item(a) for a in (c.search(query, filter="albums", limit=limit) or [])
        ][:limit]
        playlists = [
            self._playlist_item(p) for p in (c.search(query, filter="playlists", limit=limit) or [])
        ][:limit]
        tracks: list[BrowseItem] = []
        for i, t in enumerate((c.search(query, filter="songs", limit=limit) or [])[:limit]):
            if not t.get("videoId"):
                continue
            parent = BrowseItem(
                content_ref=ContentRef(service=SERVICE, kind="track", id=str(t["videoId"])),
                title=str(t.get("title") or ""),
                art=self._art(t.get("thumbnails"), f"track:{t['videoId']}"),
            )
            tracks.append(self._track_item(t, i, parent))
        return {"albums": albums, "playlists": playlists, "tracks": tracks}

    def _library_page(self, items: list[BrowseItem], limit: int, offset: int) -> BrowsePage:
        """ytmusicapi pages continuations internally up to the ``limit`` we pass; when the library
        is at least that long we cannot know the total, so ``next_offset`` follows the
        full-page rule instead of a total."""
        total = None if len(items) >= self.max_library_items else len(items)
        return _page(items[offset : offset + limit], limit, offset, total)

    def _library_albums(self, limit: int, offset: int) -> BrowsePage:
        albums = list(self._client().get_library_albums(limit=self.max_library_items) or [])
        items = [self._album_item(a) for a in albums if a.get("browseId")]
        return self._library_page(items, limit, offset)

    def _library_playlists(self, limit: int, offset: int) -> BrowsePage:
        pls = list(self._client().get_library_playlists(limit=self.max_library_items) or [])
        # ytmusicapi lists the auto "Liked Music" (LM) and "Episodes for later" too; keep LM,
        # drop containers without an id.
        items = [self._playlist_item(p) for p in pls if p.get("playlistId")]
        return self._library_page(items, limit, offset)

    def _tracks(
        self, raw: list[dict[str, Any]], parent: BrowseItem
    ) -> tuple[list[BrowseItem], bool]:
        """Playable rows only (``videoId`` present, ``isAvailable`` not false), renumbered, cut at
        ``max_tracks``."""
        playable = [t for t in raw if t.get("videoId") and t.get("isAvailable", True)]
        truncated = len(playable) > self.max_tracks
        rows = playable[: self.max_tracks]
        tracks = [self._track_item(t, i, parent) for i, t in enumerate(rows)]
        return tracks, truncated

    def _album(self, album_id: str) -> Container:
        ref = ContentRef(service=SERVICE, kind="album", id=album_id)
        try:
            a = self._client().get_album(album_id)
        except Exception as exc:  # noqa: BLE001 - ytmusicapi raises on unknown browseIds
            raise ContentNotFoundError(ref) from exc
        if not a or not a.get("title"):
            raise ContentNotFoundError(ref)
        a = {**a, "browseId": album_id}
        item = self._album_item(a)
        tracks, truncated = self._tracks(list(a.get("tracks") or []), item)
        return Container(item=item, tracks=tracks, truncated=truncated)

    def _playlist(self, playlist_id: str) -> Container:
        ref = ContentRef(service=SERVICE, kind="playlist", id=playlist_id)
        try:
            # Ask for one more than the cap so a full page is distinguishable from "more".
            p = self._client().get_playlist(playlist_id, limit=self.max_tracks + 1)
        except Exception as exc:  # noqa: BLE001
            raise ContentNotFoundError(ref) from exc
        if not p or not p.get("title"):
            raise ContentNotFoundError(ref)
        p = {**p, "playlistId": playlist_id}
        item = self._playlist_item(p)
        tracks, truncated = self._tracks(list(p.get("tracks") or []), item)
        return Container(item=item, tracks=tracks, truncated=truncated)

    def _track(self, video_id: str) -> BrowseItem:
        ref = ContentRef(service=SERVICE, kind="track", id=video_id)
        try:
            song = self._client().get_song(video_id)
        except Exception as exc:  # noqa: BLE001
            raise ContentNotFoundError(ref) from exc
        details = (song or {}).get("videoDetails") or {}
        if not details.get("videoId"):
            raise ContentNotFoundError(ref)
        thumbs = (details.get("thumbnail") or {}).get("thumbnails")
        parent = BrowseItem(content_ref=ref, title=str(details.get("title") or ""))
        return self._track_item(
            {
                "videoId": video_id,
                "title": details.get("title"),
                "artists": [{"name": details.get("author")}] if details.get("author") else [],
                "duration_seconds": details.get("lengthSeconds"),
                "thumbnails": thumbs,
            },
            0,
            parent,
        )

    # -- async surface (Catalog protocol) ---------------------------------------------------

    async def search(self, query: str, limit: int) -> dict[str, list[BrowseItem]]:
        """The three filters run concurrently on the search pool (one HTTP call each)."""
        from .search import in_search_pool

        albums, playlists, tracks = await asyncio.gather(
            in_search_pool(self._search_group, query, limit, "albums"),
            in_search_pool(self._search_group, query, limit, "playlists"),
            in_search_pool(self._search_group, query, limit, "songs"),
        )
        return {"albums": albums, "playlists": playlists, "tracks": tracks}

    async def favorite_albums(self, limit: int, offset: int) -> BrowsePage:
        return await asyncio.to_thread(self._library_albums, limit, offset)

    async def favorite_playlists(self, limit: int, offset: int) -> BrowsePage:
        return _page([], limit, offset, 0)

    async def user_playlists(self, limit: int, offset: int) -> BrowsePage:
        return await asyncio.to_thread(self._library_playlists, limit, offset)

    async def album(self, album_id: str) -> Container:
        return await asyncio.to_thread(self._album, album_id)

    async def playlist(self, playlist_id: str) -> Container:
        return await asyncio.to_thread(self._playlist, playlist_id)

    async def track(self, track_id: str) -> BrowseItem:
        return await asyncio.to_thread(self._track, track_id)


# --------------------------------------------------------------------------------------
# Fake catalog (HUB_FAKE_YTMUSIC=1)
# --------------------------------------------------------------------------------------

_FAKE_ALBUMS: list[tuple[str, str, str, int, str]] = [
    # browseId, title, artist, tracks, art name
    ("MPREb_alb1", "Paper Lanterns", "Cinder & Vale", 8, "yt-lanterns"),
    ("MPREb_alb2", "Concrete Bloom", "Ada Fern", 10, "yt-bloom"),
    ("MPREb_alb3", "Second Summer", "The Half Lights", 9, "yt-summer"),
]
_FAKE_PLAYLISTS: list[tuple[str, str, str, list[str], str]] = [
    # playlistId, title, author, album ids drawn from, art name
    ("PLyt001", "Morning run", "You", ["MPREb_alb2", "MPREb_alb3"], "yt-run"),
    ("PLyt002", "Kitchen dancing", "You", ["MPREb_alb1", "MPREb_alb3"], "yt-kitchen"),
    ("LM", "Liked Music", "You", ["MPREb_alb1", "MPREb_alb2", "MPREb_alb3"], "yt-liked"),
]
_TRACK_WORDS = ("Lantern", "Glass", "Meadow", "Wire", "Tide", "Amber", "Vellum", "Spool", "Kite")


class FakeYTMusicCatalog:
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
            duration_ms=n * 201_000,
            track_count=n,
            artist=artist,
        )

    def _tracks(self, aid: str, parent: BrowseItem, base_index: int = 0) -> list[BrowseItem]:
        row = next(r for r in _FAKE_ALBUMS if r[0] == aid)
        out: list[BrowseItem] = []
        for i in range(row[3]):
            vid = f"yt{aid[-1]}{i + 1:02d}xxxxxxx"[:11]
            out.append(
                BrowseItem(
                    content_ref=ContentRef(service=SERVICE, kind="track", id=vid),
                    title=f"{_TRACK_WORDS[i % len(_TRACK_WORDS)]} {i + 1}",
                    subtitle=row[2],
                    art=parent.art
                    if parent.content_ref.id == aid
                    else self._art(row[4], f"album:{aid}"),
                    duration_ms=170_000 + (i * 9_000) % 80_000,
                    index=base_index + i,
                    album_id=aid,
                    artist=row[2],
                    album=row[1],
                )
            )
        return out

    def _playlist_item(self, row: tuple[str, str, str, list[str], str]) -> BrowseItem:
        pid, title, author, albums, art = row
        n = 3 * len(albums)
        return BrowseItem(
            content_ref=ContentRef(service=SERVICE, kind="playlist", id=pid),
            title=title,
            subtitle=author,
            art=self._art(art, f"playlist:{pid}"),
            duration_ms=n * 190_000,
            track_count=n,
        )

    def _check(self) -> None:
        self.calls += 1
        if not self.linked:
            raise NeedsLinkError(SERVICE, f"{LABEL} is not connected. Link it in Settings.")

    async def favorite_albums(self, limit: int, offset: int) -> BrowsePage:
        self._check()
        items = [self._album_item(r) for r in _FAKE_ALBUMS]
        return _page(items[offset : offset + limit], limit, offset, len(items))

    async def favorite_playlists(self, limit: int, offset: int) -> BrowsePage:
        self._check()
        return _page([], limit, offset, 0)

    async def user_playlists(self, limit: int, offset: int) -> BrowsePage:
        self._check()
        items = [self._playlist_item(r) for r in _FAKE_PLAYLISTS]
        return _page(items[offset : offset + limit], limit, offset, len(items))

    async def album(self, album_id: str) -> Container:
        self._check()
        row = next((r for r in _FAKE_ALBUMS if r[0] == album_id), None)
        if row is None:
            raise ContentNotFoundError(ContentRef(service=SERVICE, kind="album", id=album_id))
        item = self._album_item(row)
        return Container(item=item, tracks=self._tracks(album_id, item))

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

    async def search(self, query: str, limit: int) -> dict[str, list[BrowseItem]]:
        self._check()
        q = query.casefold()
        albums = [
            self._album_item(r)
            for r in _FAKE_ALBUMS
            if q in r[1].casefold() or q in r[2].casefold()
        ]
        playlists = [self._playlist_item(r) for r in _FAKE_PLAYLISTS if q in r[1].casefold()]
        tracks: list[BrowseItem] = []
        for r in _FAKE_ALBUMS:
            item = self._album_item(r)
            tracks.extend(
                t
                for t in self._tracks(r[0], item)
                if q in t.title.casefold() or q in (t.artist or "").casefold()
            )
        return {
            "albums": albums[:limit],
            "playlists": playlists[:limit],
            "tracks": tracks[:limit],
        }

    async def track(self, track_id: str) -> BrowseItem:
        self._check()
        for row in _FAKE_ALBUMS:
            item = self._album_item(row)
            for t in self._tracks(row[0], item):
                if t.content_ref.id == track_id:
                    return t
        raise ContentNotFoundError(ContentRef(service=SERVICE, kind="track", id=track_id))
