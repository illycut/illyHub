"""YouTube Music catalog over a fake ``ytmusicapi.YTMusic`` shaped like the real responses."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from illyhub_hub.art import ArtHelper
from illyhub_hub.content import Availability, ContentNotFoundError, ContentRef, NeedsLinkError
from illyhub_hub.services.tidal import TidalService
from illyhub_hub.services.ytmusic import FakeYTMusicCatalog, YTMusicCatalog

MAIN = threading.main_thread()


def thumbs(n: int = 3) -> list[dict[str, Any]]:
    return [
        {"url": f"https://i.ytimg.com/{s}.jpg", "width": s, "height": s} for s in (60, 226, 544)
    ][:n]


class FakeYT:
    """Response shapes copied from ytmusicapi's parsers (library albums/playlists, get_playlist,
    get_album, get_song). Every call asserts it runs off the event-loop thread."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _guard(self, name: str) -> None:
        assert threading.current_thread() is not MAIN, "ytmusicapi called on the loop thread"
        self.calls.append(name)

    def get_library_albums(self, limit: int | None = 25, order: Any = None) -> list[dict]:
        self._guard("albums")
        return [
            {
                "browseId": "MPREb_A1",
                "playlistId": "OLAK5uy_1",
                "title": "Paper Lanterns",
                "type": "Album",
                "artists": [{"name": "Cinder & Vale", "id": "UC1"}],
                "year": "2024",
                "thumbnails": thumbs(),
            },
            {"title": "No id here", "artists": []},  # skipped: no browseId
        ]

    def get_library_playlists(self, limit: int | None = 25) -> list[dict]:
        self._guard("playlists")
        return [
            {
                "title": "Morning run",
                "playlistId": "PLrun",
                "thumbnails": thumbs(2),
                "count": "1,204",
                "author": [{"name": "James", "id": "UCme"}],
            },
            {"title": "Liked Music", "playlistId": "LM", "thumbnails": thumbs(1), "count": "48"},
            {"title": "Episodes for later", "thumbnails": []},  # no id: skipped
        ]

    def get_album(self, browse_id: str) -> dict:
        self._guard("album")
        if browse_id != "MPREb_A1":
            raise Exception("Invalid browseId")
        return {
            "title": "Paper Lanterns",
            "type": "Album",
            "thumbnails": thumbs(),
            "artists": [{"name": "Cinder & Vale", "id": "UC1"}],
            "year": "2024",
            "trackCount": 2,
            "duration_seconds": 400,
            "audioPlaylistId": "OLAK5uy_1",
            "tracks": [
                {
                    "videoId": "vid00000001",
                    "title": "Lantern",
                    "artists": [{"name": "Cinder & Vale", "id": "UC1"}],
                    "album": None,
                    "duration": "3:20",
                    "duration_seconds": 200,
                    "trackNumber": 1,
                },
                {
                    "videoId": "vid00000002",
                    "title": "Glass",
                    "artists": [{"name": "Cinder & Vale", "id": "UC1"}],
                    "duration": "3:20",
                    "duration_seconds": 200,
                    "trackNumber": 2,
                    "thumbnails": thumbs(1),
                },
            ],
        }

    def get_playlist(self, playlist_id: str, limit: int | None = 100, **kw: Any) -> dict:
        self._guard("playlist")
        if playlist_id != "PLrun":
            raise Exception("Unknown playlist")
        return {
            "id": "PLrun",
            "title": "Morning run",
            "thumbnails": thumbs(),
            "author": {"name": "James", "id": "UCme"},
            "trackCount": 3,
            "duration_seconds": 601,
            "tracks": [
                {
                    "videoId": "vid00000002",
                    "title": "Glass",
                    "artists": [{"name": "Cinder & Vale"}],
                    "album": {"name": "Paper Lanterns", "id": "MPREb_A1"},
                    "duration": "3:20",
                    "duration_seconds": 200,
                    "isAvailable": True,
                    "thumbnails": thumbs(1),
                },
                {
                    "videoId": "vid00000009",
                    "title": "Gone",
                    "artists": [],
                    "album": None,
                    "isAvailable": False,  # filtered out
                },
                {
                    "videoId": "vid00000003",
                    "title": "Meadow",
                    "artists": [{"name": "Ada Fern"}, {"name": "Cinder & Vale"}],
                    "album": {"name": "Concrete Bloom", "id": "MPREb_B2"},
                    "duration": "6:41",
                    "duration_seconds": None,
                },
            ],
        }

    def get_song(self, video_id: str, **kw: Any) -> dict:
        self._guard("song")
        if video_id == "missing0000":
            return {"videoDetails": {}}
        return {
            "videoDetails": {
                "videoId": video_id,
                "title": "Lantern",
                "author": "Cinder & Vale",
                "lengthSeconds": "200",
                "thumbnail": {"thumbnails": thumbs()},
            }
        }


def service(yt: FakeYT | None = None, linked: bool = True) -> tuple[TidalService, FakeYT]:
    yt = yt or FakeYT()
    catalog = YTMusicCatalog(lambda: yt, ArtHelper())
    svc = TidalService(
        catalog,
        is_linked=lambda: linked,
        availability=lambda: Availability(heos=False, sonos=True),
        service="ytmusic",
    )
    return svc, yt


async def test_library_albums_and_playlists_become_browse_items() -> None:
    svc, yt = service()
    albums = await svc.favorite_albums()
    assert [i.content_ref.model_dump() for i in albums.items] == [
        {"service": "ytmusic", "kind": "album", "id": "MPREb_A1"}
    ]
    a = albums.items[0]
    assert a.title == "Paper Lanterns" and a.subtitle == "Cinder & Vale" and a.artist == a.subtitle
    assert a.availability == Availability(heos=False, sonos=True)
    pls = await svc.user_playlists()
    assert [(p.content_ref.id, p.title, p.subtitle, p.track_count) for p in pls.items] == [
        ("PLrun", "Morning run", "James", 1204),
        ("LM", "Liked Music", None, 48),
    ]
    assert (await svc.favorite_playlists()).items == []
    # cached: a second read does not hit the client
    await svc.favorite_albums()
    assert yt.calls.count("albums") == 1


async def test_album_container_tracks_carry_video_ids_and_album_context() -> None:
    svc, _yt = service()
    c = await svc.container(ContentRef(service="ytmusic", kind="album", id="MPREb_A1"))
    assert c.item.content_ref.id == "MPREb_A1" and c.item.track_count == 2
    assert [(t.content_ref.id, t.index, t.album_id, t.album) for t in c.tracks] == [
        ("vid00000001", 0, "MPREb_A1", "Paper Lanterns"),
        ("vid00000002", 1, "MPREb_A1", "Paper Lanterns"),
    ]
    assert c.tracks[0].duration_ms == 200_000
    item, playable = await svc.tracks_for(c.item.content_ref)
    assert item.title == "Paper Lanterns"
    assert [p.service for p in playable] == ["ytmusic", "ytmusic"]
    assert playable[1].track_id == "vid00000002" and playable[1].album_id == "MPREb_A1"


async def test_playlist_filters_unavailable_and_parses_duration_strings() -> None:
    svc, _yt = service()
    c = await svc.container(ContentRef(service="ytmusic", kind="playlist", id="PLrun"))
    assert [t.content_ref.id for t in c.tracks] == ["vid00000002", "vid00000003"]
    assert [t.index for t in c.tracks] == [0, 1]
    meadow = c.tracks[1]
    assert meadow.artist == "Ada Fern, Cinder & Vale" and meadow.album == "Concrete Bloom"
    assert meadow.duration_ms == 401_000  # "6:41" parsed when duration_seconds is None
    _item, playable = await svc.tracks_for(c.item.content_ref)
    assert playable[0].playlist_id == "PLrun"


async def test_single_track_via_get_song_and_not_found_paths() -> None:
    svc, _yt = service()
    item, playable = await svc.tracks_for(
        ContentRef(service="ytmusic", kind="track", id="vid00000001")
    )
    assert item.title == "Lantern" and item.artist == "Cinder & Vale"
    assert playable[0].duration_ms == 200_000
    with pytest.raises(ContentNotFoundError):
        await svc.tracks_for(ContentRef(service="ytmusic", kind="track", id="missing0000"))
    with pytest.raises(ContentNotFoundError):
        await svc.container(ContentRef(service="ytmusic", kind="album", id="MPREb_nope"))
    with pytest.raises(ContentNotFoundError):
        await svc.container(ContentRef(service="ytmusic", kind="playlist", id="PLnope"))


async def test_unlinked_service_raises_needs_link_with_the_service_label() -> None:
    svc, _yt = service(linked=False)
    with pytest.raises(NeedsLinkError) as ei:
        await svc.user_playlists()
    assert ei.value.service == "ytmusic"
    assert ei.value.message == "YouTube Music is not connected. Link it in Settings."


async def test_fake_catalog_is_deterministic_and_toggles_with_linked() -> None:
    fake = FakeYTMusicCatalog(ArtHelper())
    svc = TidalService(
        fake,
        is_linked=lambda: fake.linked,
        availability=lambda: Availability(sonos=True),
        service="ytmusic",
    )
    albums = (await svc.favorite_albums()).items
    assert [a.title for a in albums] == ["Paper Lanterns", "Concrete Bloom", "Second Summer"]
    pls = (await svc.user_playlists()).items
    assert [p.content_ref.id for p in pls] == ["PLyt001", "PLyt002", "LM"]
    c = await svc.container(pls[0].content_ref)
    assert len(c.tracks) == 6 and [t.index for t in c.tracks] == list(range(6))
    assert all(len(t.content_ref.id) == 11 for t in c.tracks)  # YouTube videoId shape
    track = c.tracks[0]
    single, _ = await svc.tracks_for(track.content_ref)
    assert single.title == track.title
    with pytest.raises(ContentNotFoundError):
        await svc.container(ContentRef(service="ytmusic", kind="album", id="MPREb_zzz"))
    fake.linked = False
    with pytest.raises(NeedsLinkError):
        await svc.favorite_albums()
