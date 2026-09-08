"""YouTube Music on Sonos: ref builders, account-serial discovery, play path (mock-verified)."""

from __future__ import annotations

import pytest

from illyhub_hub.adapters.base import ContentUnavailableError, PlayableTrack
from illyhub_hub.adapters.sonos import (
    YTMUSIC_DESC,
    YTMUSIC_SERVICE_TYPE,
    build_ytmusic_didl,
    discover_ytmusic_sn,
    ytmusic_item_id,
    ytmusic_parent_id,
    ytmusic_ref_from_uri,
    ytmusic_track_uri,
)
from illyhub_hub.config import Settings
from illyhub_hub.content import ContentRef
from illyhub_hub.state import StateStore
from test_sonos import Harness, wait_for


def test_ref_builders_follow_the_documented_assumptions() -> None:
    assert YTMUSIC_SERVICE_TYPE == "72711"
    assert ytmusic_track_uri("dQw4w9WgXcQ", "4") == (
        "x-sonos-http:sonos-track:dQw4w9WgXcQ.mp4?sid=284&flags=8232&sn=4"
    )
    assert ytmusic_track_uri("a b", "4", "custom:{id}?sn={sn}") == "custom:a%20b?sn=4"
    assert ytmusic_item_id("dQw4w9WgXcQ") == "00032020sonos-track:dQw4w9WgXcQ"
    assert ytmusic_parent_id("MPREb_A1", None) == "0004206calbum:MPREb_A1"
    assert ytmusic_parent_id("MPREb_A1", "PLrun") == "0006206cplaylist:PLrun"
    assert ytmusic_parent_id(None, None) == "00032020"


def test_didl_is_a_real_soco_music_track_with_the_account_descriptor() -> None:
    from soco.data_structures import DidlMusicTrack

    t = PlayableTrack(
        service="ytmusic",
        track_id="dQw4w9WgXcQ",
        title="Lantern",
        artist="Cinder & Vale",
        album="Paper Lanterns",
        album_id="MPREb_A1",
    )
    didl = build_ytmusic_didl(t, "4")
    assert isinstance(didl, DidlMusicTrack)
    assert didl.title == "Lantern" and didl.creator == "Cinder & Vale"
    assert didl.desc == YTMUSIC_DESC == "SA_RINCON72711_X_#Svc72711-0-Token"
    assert didl.resources[0].uri.endswith("sid=284&flags=8232&sn=4")
    assert didl.parent_id == "0004206calbum:MPREb_A1"
    xml = didl.to_element().attrib
    assert xml["id"] == "00032020sonos-track:dQw4w9WgXcQ"


def test_ref_from_uri_recognizes_only_youtube_music_uris() -> None:
    uri = "x-sonos-http:sonos-track:dQw4w9WgXcQ.mp4?sid=284&flags=8232&sn=4"
    assert ytmusic_ref_from_uri(uri) == ContentRef(
        service="ytmusic", kind="track", id="dQw4w9WgXcQ"
    )
    hls = "x-sonosapi-hls-static:dQw4w9WgXcQ?sid=284&flags=8&sn=4"
    assert ytmusic_ref_from_uri(hls) is not None
    assert ytmusic_ref_from_uri("x-sonos-http:track/12345.flac?sid=174&sn=3") is None
    assert ytmusic_ref_from_uri("x-sonos-http:sonos-track:dQw4w9WgXcQ.mp4?sid=9&sn=1") is None
    assert ytmusic_ref_from_uri(None) is None


def test_discover_ytmusic_sn_maps_service_type_72711() -> None:
    class Acct:
        def __init__(self, service_type: str) -> None:
            self.service_type = service_type

    class Accounts:
        @staticmethod
        def get_accounts(zone):
            return {"3": Acct("44551"), "9": Acct("72711")}

    class NoYT:
        @staticmethod
        def get_accounts(zone):
            return {"3": Acct("44551")}

    assert discover_ytmusic_sn("zone", account_class=Accounts) == "9"
    assert discover_ytmusic_sn("zone", account_class=NoYT) is None
    assert discover_ytmusic_sn(None, account_class=Accounts) is None


async def test_play_content_ytmusic_builds_a_queue_only_when_linked(
    store: StateStore, settings: Settings
) -> None:
    h = Harness()
    a = h.adapter(store, settings)
    await a.connect()
    await wait_for(lambda: a.status().state == "connected")
    k = h.zones[0]
    built: list = []
    k.clear_queue = lambda: k._rec("clear_queue")  # type: ignore[attr-defined]
    k.add_multiple_to_queue = lambda items: built.extend(items)  # type: ignore[attr-defined]
    k.play_from_queue = lambda i: k._rec("play_from_queue", i)  # type: ignore[attr-defined]
    tracks = [
        PlayableTrack(
            service="ytmusic", track_id=f"vid0000000{i}", title=f"T{i}", playlist_id="PLrun"
        )
        for i in range(3)
    ]
    ref = ContentRef(service="ytmusic", kind="playlist", id="PLrun")
    assert a.service_linked("ytmusic") is False
    with pytest.raises(
        ContentUnavailableError, match="does not know this speaker's YouTube Music account"
    ):
        await a.play_content("sonos-RINCON_K", ref, tracks, 0)
    a.settings = settings.model_copy(
        update={"sonos_ytmusic_sn": "9", "sonos_ytmusic_uri": "yt://{id}?sn={sn}"}
    )
    assert a.service_linked("ytmusic") is True
    await a.play_content("sonos-RINCON_K", ref, tracks, start_index=1)
    assert [c for c in k.calls if c[0] == "play_from_queue"] == [("play_from_queue", 1)]
    assert len(built) == 3
    assert built[0].resources[0].uri == "yt://vid00000000?sn=9"  # template override honoured
    assert built[0].parent_id == "0006206cplaylist:PLrun"
    # Tidal still routes to its own builder and Pandora refs are refused.
    with pytest.raises(ContentUnavailableError):
        await a.play_content(
            "sonos-RINCON_K", ContentRef(service="pandora", kind="station", id="x"), tracks, 0
        )
    await a.disconnect()
