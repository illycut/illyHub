from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import pytest

from illyhub_hub.art import ArtHelper
from illyhub_hub.content import Availability, ContentNotFoundError, ContentRef, NeedsLinkError
from illyhub_hub.services.tidal import FakeTidalCatalog, TidalCatalog, TidalService

# --------------------------------------------------------------------------------------
# A fake tidalapi object graph: every attribute access asserts it is off the loop thread,
# like the SoCo LazyZone double, so the catalog can never touch tidalapi on the loop.
# --------------------------------------------------------------------------------------


def _guard() -> None:
    assert threading.current_thread() is not threading.main_thread(), "tidalapi on loop thread"


class Lazy:
    def __init__(self, **attrs: Any) -> None:
        self.__dict__["_attrs"] = attrs

    def __getattr__(self, name: str) -> Any:
        _guard()
        try:
            return self.__dict__["_attrs"][name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def artist(name: str) -> Lazy:
    return Lazy(name=name)


class ObjectNotFound(Exception):  # noqa: N818 - must match the tidalapi class name
    """Same class name as tidalapi.exceptions.ObjectNotFound; the service matches by name."""


def paged(items: list[Any]):
    """A tidalapi-style ``tracks(limit=, offset=)`` pager over ``items``."""

    def fetch(limit: int | None = None, offset: int = 0, **_: Any) -> list[Any]:
        _guard()
        end = len(items) if limit is None else offset + limit
        return items[offset:end]

    return fetch


def album(
    aid: int, name: str, who: str, cover: str, n: int, tracks: list[Lazy] | None = None
) -> Lazy:
    a = Lazy(id=aid, name=name, artist=artist(who), cover=cover, duration=n * 200, num_tracks=n)
    a.__dict__["_attrs"]["tracks"] = paged(tracks or [])
    return a


def track(tid: int, name: str, alb: Lazy, who: str, seconds: int) -> Lazy:
    return Lazy(id=tid, name=name, album=alb, artist=artist(who), duration=seconds)


def playlist(pid: str, name: str, creator: str | None, tracks: list[Lazy]) -> Lazy:
    p = Lazy(
        id=pid,
        name=name,
        creator=artist(creator) if creator else None,
        description="desc",
        square_picture="1111-2222",
        picture=None,
        duration=len(tracks) * 200,
        num_tracks=len(tracks),
    )
    p.__dict__["_attrs"]["tracks"] = paged(tracks)
    return p


@dataclass
class FakeTidalSession:
    albums: dict[str, Lazy] = field(default_factory=dict)
    playlists: dict[str, Lazy] = field(default_factory=dict)
    user_playlists: list[Lazy] = field(default_factory=list)
    fav_albums: list[Lazy] = field(default_factory=list)
    fav_playlists: list[Lazy] = field(default_factory=list)

    @property
    def user(self) -> Any:
        _guard()
        outer = self

        class Favorites:
            def albums(self, limit: int = 50, offset: int = 0):
                _guard()
                return outer.fav_albums[offset : offset + limit]

            def playlists(self, limit: int = 50, offset: int = 0):
                _guard()
                return outer.fav_playlists[offset : offset + limit]

        class User:
            favorites = Favorites()

            def playlists(self):
                _guard()
                return list(outer.user_playlists)

        return User()

    tracks: dict[str, Lazy] = field(default_factory=dict)

    def album(self, album_id: str) -> Any:
        _guard()
        if str(album_id) not in self.albums:
            raise ObjectNotFound("Object not found")  # like tidalapi on a 404
        return self.albums[str(album_id)]

    def playlist(self, playlist_id: str) -> Any:
        _guard()
        if str(playlist_id) not in self.playlists:
            raise ObjectNotFound("Object not found")
        return self.playlists[str(playlist_id)]

    def track(self, track_id: str, with_album: bool = False) -> Any:
        _guard()
        if str(track_id) not in self.tracks:
            raise ObjectNotFound("Object not found")
        return self.tracks[str(track_id)]


def library() -> FakeTidalSession:
    warm = album(101, "Warm Glow", "Analog Heart", "aaaa-bbbb-cccc", 2)
    t1 = track(10101, "Signal", warm, "Analog Heart", 213)
    t2 = track(10102, "Carrier", warm, "Analog Heart", 187)
    warm.__dict__["_attrs"]["tracks"] = paged([t1, t2])
    night = album(102, "Night Drive", "Neon Static", "dddd-eeee", 1)
    pl = playlist("p-1", "Dinner party", None, [t2, t1])
    return FakeTidalSession(
        albums={"101": warm, "102": night},
        playlists={"p-1": pl},
        tracks={"10101": t1, "10102": t2},
        user_playlists=[pl],
        fav_albums=[warm, night],
        fav_playlists=[playlist("p-9", "Tidal Rising", "Tidal", [t1])],
    )


def real_service(session: FakeTidalSession, *, linked: bool = True, ttl: float = 300.0):
    avail = Availability(heos=True, sonos=False)
    calls: list[str] = []

    async def before() -> None:
        calls.append("before")

    svc = TidalService(
        TidalCatalog(lambda: session, ArtHelper()),
        is_linked=lambda: linked,
        availability=lambda: avail,
        cache_ttl_s=ttl,
        before=before,
    )
    return svc, calls


async def test_real_catalog_reads_tidalapi_off_the_loop_and_maps_items() -> None:
    svc, calls = real_service(library())
    page = await svc.favorite_albums()
    assert [i.title for i in page.items] == ["Warm Glow", "Night Drive"]
    warm = page.items[0]
    assert warm.content_ref == ContentRef(service="tidal", kind="album", id="101")
    assert warm.subtitle == "Analog Heart" and warm.track_count == 2 and warm.duration_ms == 400_000
    assert warm.availability == Availability(heos=True, sonos=False)  # stamped by the service
    assert warm.art.url is not None  # placeholder ref without a cache, never a device URL
    assert calls == ["before"]  # token refresh hook ran before the uncached call

    c = await svc.container(ContentRef(service="tidal", kind="album", id="101"))
    assert [t.title for t in c.tracks] == ["Signal", "Carrier"]
    assert (
        c.tracks[1].index == 1
        and c.tracks[1].album_id == "101"
        and c.tracks[1].album == "Warm Glow"
    )
    assert c.tracks[0].duration_ms == 213_000

    mine = await svc.user_playlists()
    assert mine.total == 1 and mine.items[0].content_ref.kind == "playlist"
    favs = await svc.favorite_playlists()
    assert favs.items[0].subtitle == "Tidal"


async def test_tidalapi_guard_fires_if_touched_on_the_loop() -> None:
    with pytest.raises(AssertionError, match="tidalapi on loop thread"):
        library().album("101")


async def test_cache_ttl_and_refresh() -> None:
    session = library()
    clock = {"t": 1000.0}
    avail = Availability(heos=True, sonos=True)
    svc = TidalService(
        TidalCatalog(lambda: session, ArtHelper()),
        is_linked=lambda: True,
        availability=lambda: avail,
        cache_ttl_s=300.0,
        clock=lambda: clock["t"],
    )
    a = await svc.favorite_albums()
    session.fav_albums.pop()  # library changed underneath
    b = await svc.favorite_albums()
    assert len(a.items) == len(b.items) == 2  # served from cache
    clock["t"] += 301
    assert len((await svc.favorite_albums()).items) == 1  # expired
    session.fav_albums.pop()
    assert svc.refresh() >= 1
    assert (await svc.favorite_albums()).items == []


async def test_needs_link_and_not_found() -> None:
    svc, _ = real_service(library(), linked=False)
    with pytest.raises(NeedsLinkError) as exc:
        await svc.favorite_albums()
    assert exc.value.service == "tidal"
    svc, _ = real_service(library())
    with pytest.raises(ContentNotFoundError):
        await svc.container(ContentRef(service="tidal", kind="album", id="999"))
    with pytest.raises(ContentNotFoundError):
        await svc.container(ContentRef(service="tidal", kind="station", id="x"))


async def test_tracks_for_album_playlist_and_bare_track() -> None:
    svc, _ = real_service(library())
    item, tracks = await svc.tracks_for(ContentRef(service="tidal", kind="playlist", id="p-1"))
    assert item.title == "Dinner party"
    assert [t.track_id for t in tracks] == ["10102", "10101"]
    assert tracks[0].playlist_id == "p-1" and tracks[0].album_id == "101"
    _item, tracks = await svc.tracks_for(ContentRef(service="tidal", kind="album", id="101"))
    assert tracks[0].playlist_id is None and tracks[0].service == "tidal"
    item, tracks = await svc.tracks_for(ContentRef(service="tidal", kind="track", id="10102"))
    assert len(tracks) == 1 and tracks[0].track_id == "10102"
    assert item.title == "Carrier" and item.artist == "Analog Heart" and item.album_id == "101"
    assert tracks[0].album == "Warm Glow" and tracks[0].duration_ms == 187_000
    with pytest.raises(ContentNotFoundError):
        await svc.tracks_for(ContentRef(service="tidal", kind="track", id="777"))


async def test_track_pages_are_walked_until_a_short_page() -> None:
    from illyhub_hub.services.tidal import TRACK_PAGE

    session = library()
    big = album(200, "Big Box Set", "Various", "ffff", 250)
    items = [track(20000 + i, f"Cut {i}", big, "Various", 100) for i in range(250)]
    big.__dict__["_attrs"]["tracks"] = paged(items)
    session.albums["200"] = big
    svc, _ = real_service(session)
    c = await svc.container(ContentRef(service="tidal", kind="album", id="200"))
    assert len(c.tracks) == 250 and c.tracks[-1].index == 249
    assert TRACK_PAGE == 100


async def test_favorites_page_more_is_judged_against_the_server_cap() -> None:
    from illyhub_hub.services.tidal import PAGE_MAX

    session = library()
    session.fav_albums = [album(300 + i, f"A{i}", "X", "c", 1) for i in range(50)]
    svc, _ = real_service(session)
    page = await svc.favorite_albums(limit=200)  # client asks for more than Tidal serves
    assert page.limit == PAGE_MAX == 50 and len(page.items) == 50
    assert page.next_offset == 50  # a full server page means there may be more
    session.fav_albums = session.fav_albums[:10]
    svc.refresh()
    page = await svc.favorite_albums(limit=50)
    assert page.next_offset is None  # short page: the end


async def test_cache_is_capped_and_purges_expired_entries() -> None:
    from illyhub_hub.services.tidal import CACHE_MAX_ENTRIES

    session = library()
    for i in range(300):
        session.albums[str(1000 + i)] = album(1000 + i, f"A{i}", "X", "c", 0)
    clock = {"t": 0.0}
    svc = TidalService(
        TidalCatalog(lambda: session, ArtHelper()),
        is_linked=lambda: True,
        availability=lambda: Availability(),
        cache_ttl_s=300.0,
        clock=lambda: clock["t"],
    )
    for i in range(300):
        await svc.container(ContentRef(service="tidal", kind="album", id=str(1000 + i)))
    assert len(svc._cache) <= CACHE_MAX_ENTRIES
    clock["t"] = 1000.0
    await svc.container(ContentRef(service="tidal", kind="album", id="1000"))
    assert len(svc._cache) == 1  # everything else expired and was purged on write


async def test_fake_catalog_is_deterministic_and_toggleable() -> None:
    cat = FakeTidalCatalog(ArtHelper())
    avail = Availability(heos=True, sonos=True)
    svc = TidalService(cat, is_linked=lambda: cat.linked, availability=lambda: avail)
    albums = await svc.favorite_albums(limit=4)
    assert len(albums.items) == 4 and albums.total == 6 and albums.next_offset == 4
    rest = await svc.favorite_albums(limit=4, offset=4)
    assert len(rest.items) == 2 and rest.next_offset is None
    c = await svc.container(ContentRef(service="tidal", kind="album", id="101"))
    assert len(c.tracks) == 9 and c.tracks[3].index == 3 and c.tracks[3].content_ref.id == "10104"
    p = await svc.container(ContentRef(service="tidal", kind="playlist", id="p-2"))
    assert len(p.tracks) == 6 and [t.index for t in p.tracks] == list(range(6))
    mine = await svc.user_playlists()
    assert {i.subtitle for i in mine.items} == {"You"}
    with pytest.raises(ContentNotFoundError):
        await svc.container(ContentRef(service="tidal", kind="playlist", id="nope"))
    t = await cat.track("10104")
    assert t.title == "Harmonic 4" and t.album_id == "101" and t.album == "Warm Glow"
    with pytest.raises(ContentNotFoundError):
        await cat.track("99999")
    cat.linked = False
    with pytest.raises(NeedsLinkError):
        await svc.user_playlists()


async def test_refresh_failure_mid_browse_surfaces_as_needs_link() -> None:
    session = library()
    calls = {"n": 0}

    async def before() -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            raise NeedsLinkError("tidal", "Tidal session expired. Link it again in Settings.")

    svc = TidalService(
        TidalCatalog(lambda: session, ArtHelper()),
        is_linked=lambda: True,
        availability=lambda: Availability(),
        before=before,
    )
    assert len((await svc.favorite_albums()).items) == 2
    with pytest.raises(NeedsLinkError, match="expired"):
        await svc.user_playlists()  # uncached → refresh hook → expired


async def test_unlink_during_in_flight_browse_does_not_leak_a_linked_result() -> None:
    import asyncio

    session = library()
    linked = {"v": True}
    gate = asyncio.Event()

    class SlowCatalog(TidalCatalog):
        async def favorite_albums(self, limit: int, offset: int):
            await gate.wait()
            return await super().favorite_albums(limit, offset)

    svc = TidalService(
        SlowCatalog(lambda: session, ArtHelper()),
        is_linked=lambda: linked["v"],
        availability=lambda: Availability(),
    )
    task = asyncio.create_task(svc.favorite_albums())
    await asyncio.sleep(0)
    linked["v"] = False  # unlink while the request is in flight
    svc.refresh()
    gate.set()
    page = await task  # the in-flight call completes with the data it already had...
    assert len(page.items) == 2
    with pytest.raises(NeedsLinkError):
        await svc.favorite_albums()  # ...but nothing after it is served, cached or not
