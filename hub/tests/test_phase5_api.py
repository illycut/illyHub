"""Phase 5 API: Pandora stations per system, merged list, station play, home, scenarios."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from illyhub_hub.api import HubRuntime, create_app
from illyhub_hub.config import Settings

HEOS_SIDE = "heos:heos-1"
SONOS_SIDE = "sonos:sonos-gRINCON_KITCHEN:1"


def fake_settings(tmp_path: Path, **kw) -> Settings:
    kw.setdefault("fake_tidal", True)
    kw.setdefault("fake_pandora", True)
    return Settings(
        _env_file=None,
        fake_devices=True,
        position_poll_s=0.02,
        command_coalesce_s=0.0,
        data_dir=str(tmp_path / "data"),
        app_dir=str(tmp_path / "no-app"),
        fonts_dir=str(tmp_path / "no-fonts"),
        **kw,
    )


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, HubRuntime]]:
    app = create_app(fake_settings(tmp_path))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c, app.state.runtime


def flags(avail: dict) -> dict:
    """Vendor flags only; `reasons` are asserted where the reason matters."""
    return {"heos": avail["heos"], "sonos": avail["sonos"]}


def ref(key: str) -> dict[str, str]:
    return {"service": "pandora", "kind": "station", "id": key}


async def test_merged_station_list_and_home_section(client) -> None:
    c, _rt = client
    page = (await c.get("/api/browse/pandora/stations")).json()
    titles = [i["title"] for i in page["items"]]
    assert titles == [
        "90s Alternative",
        "Chill Radio",
        "Focus Flow",
        "Jazz Nights",
        "Living Room Mix",
        "Patio Party",
    ]
    by_title = {i["title"]: i for i in page["items"]}
    assert by_title["Chill Radio"]["content_ref"] == ref("chill-radio")
    assert flags(by_title["Chill Radio"]["availability"]) == {"heos": True, "sonos": True}
    assert flags(by_title["Living Room Mix"]["availability"]) == {"heos": True, "sonos": False}
    assert flags(by_title["Patio Party"]["availability"]) == {"heos": False, "sonos": True}
    assert by_title["Living Room Mix"]["availability"]["reasons"] == {
        "heos": None,
        "sonos": "not_in_account",
    }
    assert by_title["Jazz Nights"]["art"]["url"].startswith("/api/art/")
    assert page["total"] == 6 and page["next_offset"] is None and page["limit"] == 500
    assert page["linked"] == {"heos": True, "sonos": True}
    home = (await c.get("/api/home")).json()
    assert [i["title"] for i in home["stations"]["items"]] == titles
    assert home["stations"]["needs_link"] == []
    assert home["stations"]["linked"] == {"heos": True, "sonos": True}


async def test_play_station_sets_radio_now_playing_and_records_history(client) -> None:
    c, rt = client
    r = await c.post("/api/play", json={"target": HEOS_SIDE, "content_ref": ref("chill-radio")})
    assert r.status_code == 200, r.text
    ack = r.json()
    assert ack["ok"] and ack["action"] == "play_station" and ack["applied"] == [HEOS_SIDE]
    assert ack["warnings"] == []
    np = rt.store.state.now_playing[HEOS_SIDE]
    assert np.source == "pandora" and np.album == "Chill Radio" and np.title == "Ember 1"
    assert np.seekable is False and np.supports_prev is False and np.supports_next is True
    assert np.content_ref is not None and np.content_ref.id == "chill-radio"
    assert rt.store.state.sides[HEOS_SIDE].play_state == "play"
    caps = rt.store.state.sides[HEOS_SIDE].capabilities
    assert caps.supports_prev is False and caps.supports_seek is False
    # skip is allowed (Pandora skips), back and seek are not
    r = await c.post("/api/transport/next", json={"target": HEOS_SIDE})
    assert r.status_code == 200 and rt.store.state.now_playing[HEOS_SIDE].title == "Drift 2"
    assert rt.store.state.now_playing[HEOS_SIDE].content_ref.id == "chill-radio"
    assert (await c.post("/api/transport/prev", json={"target": HEOS_SIDE})).status_code == 409
    assert (
        await c.post("/api/seek", json={"target": HEOS_SIDE, "position_ms": 1000})
    ).status_code == 409
    # history + recents
    hist = (await c.get("/api/history")).json()["items"]
    assert hist[0]["content_ref"] == ref("chill-radio") and hist[0]["title"] == "Chill Radio"
    assert hist[0]["last_targets"] == [HEOS_SIDE]
    home = (await c.get("/api/home")).json()
    assert home["recents"][0]["content_ref"]["kind"] == "station"
    # the scripted scenario advances every playing station
    r = await c.post("/api/dev/fake/station_track_change")
    assert r.json()["result"] == {"advanced": ["heos"]}
    assert rt.store.state.now_playing[HEOS_SIDE].title == "Halo 3"


async def test_second_pandora_stream_carries_a_concurrent_warning(client) -> None:
    c, _rt = client
    await c.post("/api/play", json={"target": HEOS_SIDE, "content_ref": ref("chill-radio")})
    ack = (
        await c.post("/api/play", json={"target": SONOS_SIDE, "content_ref": ref("jazz-nights")})
    ).json()
    assert ack["ok"] and ack["applied"] == [SONOS_SIDE]
    assert ack["warnings"] == [
        {
            "code": "pandora_concurrent",
            "message": "Pandora usually allows one stream per account; the other room may pause.",
            "target": HEOS_SIDE,
        }
    ]
    # two sides in one request also warn, with no specific other room
    ack = (
        await c.post("/api/play", json={"target": "all", "content_ref": ref("focus-flow")})
    ).json()
    assert ack["ok"] and ack["warnings"][0]["target"] is None


async def test_station_only_on_one_vendor_fails_that_side_only(client) -> None:
    c, rt = client
    r = await c.post("/api/play", json={"target": "all", "content_ref": ref("living-room-mix")})
    ack = r.json()
    assert r.status_code == 200 and ack["ok"] is True
    assert HEOS_SIDE in ack["applied"] and SONOS_SIDE not in ack["applied"]
    codes = {p["target"]: p["code"] for p in ack["partial"]}
    assert codes[SONOS_SIDE] == "not_available_on_side"
    assert "isn't in the Sonos Pandora account" in next(
        p["message"] for p in ack["partial"] if p["target"] == SONOS_SIDE
    )
    assert rt.store.state.now_playing[HEOS_SIDE].album == "Living Room Mix"
    r = await c.post("/api/play", json={"target": SONOS_SIDE, "content_ref": ref("nope")})
    assert r.status_code == 404 and r.json()["code"] == "not_found"


async def test_unlink_per_vendor_then_everywhere(client) -> None:
    c, _rt = client
    r = await c.post("/api/dev/fake/unlink_pandora?vendor=sonos")
    assert r.json()["result"] == {"pandora_linked": {"heos": True, "sonos": False}}
    # No refresh call: the link scenario drops the station cache itself.
    items = (await c.get("/api/browse/pandora/stations")).json()["items"]
    assert all(i["availability"]["sonos"] is False for i in items)
    assert "Patio Party" not in {i["title"] for i in items}
    r = await c.post("/api/play", json={"target": SONOS_SIDE, "content_ref": ref("chill-radio")})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_available_on_side"
    assert "link it in the Sonos app" in r.json()["error"]["message"]
    assert (await c.post("/api/dev/fake/unlink_pandora?vendor=tv")).status_code == 400
    r = await c.post("/api/dev/fake/unlink_pandora")
    assert r.json()["result"] == {"pandora_linked": {"heos": False, "sonos": False}}
    r = await c.get("/api/browse/pandora/stations")
    assert r.status_code == 409 and r.json()["code"] == "needs_link"
    assert r.json()["service"] == "pandora"
    home = (await c.get("/api/home")).json()
    assert home["stations"] == {
        "items": [],
        "needs_link": ["pandora"],
        "error": None,
        "linked": {"heos": False, "sonos": False},
    }
    settings = (await c.get("/api/settings")).json()
    pandora = next(a for a in settings["accounts"] if a["service"] == "pandora")
    assert pandora["linked"] is False and pandora["state"] == "unlinked"
    assert pandora["linked_by_vendor"] == {"heos": False, "sonos": False}
    r = await c.post("/api/dev/fake/link_pandora?vendor=heos")
    assert r.json()["result"]["pandora_linked"] == {"heos": True, "sonos": False}
    page = (await c.get("/api/browse/pandora/stations")).json()
    assert len(page["items"]) == 5 and page["linked"] == {"heos": True, "sonos": False}
    # unknown scenario is 404 even with a bad vendor; bad vendor on a known scenario is 400
    assert (await c.post("/api/dev/fake/nope?vendor=tv")).status_code == 404


async def test_stations_are_not_syncable_and_non_station_pandora_refs_404(client) -> None:
    c, _rt = client
    r = await c.post(
        "/api/sync/play",
        json={
            "content_ref": ref("chill-radio"),
            "heos_target": HEOS_SIDE,
            "sonos_target": SONOS_SIDE,
        },
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "unsupported_content"
    r = await c.post(
        "/api/play",
        json={
            "target": HEOS_SIDE,
            "content_ref": {"service": "pandora", "kind": "album", "id": "x"},
        },
    )
    assert r.status_code == 404


async def test_without_fake_pandora_the_section_is_empty_but_linked(tmp_path: Path) -> None:
    app = create_app(fake_settings(tmp_path, fake_pandora=False))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            home = (await c.get("/api/home")).json()
            assert home["stations"] == {
                "items": [],
                "needs_link": [],
                "error": None,
                "linked": {"heos": True, "sonos": True},
            }
            page = (await c.get("/api/browse/pandora/stations")).json()
            assert page["items"] == [] and page["total"] == 0


async def test_auth_fault_on_one_vendor_is_distinct_from_not_linked_and_empty(client) -> None:
    c, _rt = client
    r = await c.post("/api/dev/fake/pandora_auth_fault?vendor=sonos")
    assert r.json()["result"] == {"pandora_auth_fault": {"heos": False, "sonos": True}}
    page = (await c.get("/api/browse/pandora/stations")).json()
    assert page["linked"] == {"heos": True, "sonos": False}
    assert all(i["availability"]["sonos"] is False for i in page["items"])
    settings = (await c.get("/api/settings")).json()
    pandora = next(a for a in settings["accounts"] if a["service"] == "pandora")
    assert pandora["linked"] is True  # the Sonos app still has the account
    assert pandora["linked_by_vendor"] == {"heos": True, "sonos": False}
    assert pandora["last_error"].startswith("Sonos: Pandora on Sonos needs to be signed in again")
    # playing on the faulted side: the message says "sign in again", never "isn't in the account"
    r = await c.post("/api/play", json={"target": SONOS_SIDE, "content_ref": ref("chill-radio")})
    assert r.status_code == 409
    msg = r.json()["error"]["message"]
    assert "sign in again in the Sonos app" in msg and "isn't in" not in msg
    # both sides faulted: needs_link with the auth message, not a 500 and not "empty"
    await c.post("/api/dev/fake/pandora_auth_fault")
    r = await c.get("/api/browse/pandora/stations")
    assert r.status_code == 409 and r.json()["code"] == "needs_link"
    assert "signed in again" in r.json()["message"]
    home = (await c.get("/api/home")).json()
    assert home["stations"]["needs_link"] == ["pandora"]
    assert home["stations"]["linked"] == {"heos": False, "sonos": False}
    await c.post("/api/dev/fake/pandora_auth_ok")
    assert (await c.get("/api/browse/pandora/stations")).json()["linked"] == {
        "heos": True,
        "sonos": True,
    }


async def test_failed_station_play_has_no_warning_and_vendor_errors_land_in_partial(client) -> None:
    c, rt = client
    await c.post("/api/play", json={"target": HEOS_SIDE, "content_ref": ref("chill-radio")})

    async def boom(*_a, **_k):
        raise RuntimeError("SoCo: UPnP 714")

    rt.fakes.sonos.play_station = boom  # type: ignore[method-assign]
    r = await c.post("/api/play", json={"target": SONOS_SIDE, "content_ref": ref("jazz-nights")})
    ack = r.json()
    assert r.status_code == 502 and ack["ok"] is False
    assert (
        ack["error"]["code"] == "vendor_error"
        and "didn't start on Kitchen + Patio" in ack["error"]["message"]
    )
    assert ack["warnings"] == []  # nothing started, so no concurrent-stream note
    r = await c.post("/api/play", json={"target": "all", "content_ref": ref("focus-flow")})
    ack = r.json()
    den_side = f"heos:{rt.store.state.players['heos-2'].id}"
    assert ack["ok"] is True and set(ack["applied"]) == {HEOS_SIDE, den_side}
    assert [p["code"] for p in ack["partial"]] == ["vendor_error"]
    # Two rooms started (both HEOS), so the concurrent note fires with no specific other room.
    assert len(ack["warnings"]) == 1 and ack["warnings"][0]["target"] is None
    # A single side that fails produces no warning even though another room already streams.
    r = await c.post("/api/play", json={"target": SONOS_SIDE, "content_ref": ref("focus-flow")})
    assert r.json()["warnings"] == [] and r.json()["ok"] is False


async def test_concurrent_warning_prefers_same_vendor_room_and_counts_paused_rooms(client) -> None:
    c, rt = client
    den = rt.store.state.players["heos-2"]
    den_side = f"heos:{den.id}"
    await c.post("/api/play", json={"target": den_side, "content_ref": ref("chill-radio")})
    await c.post("/api/play", json={"target": SONOS_SIDE, "content_ref": ref("jazz-nights")})
    # Den is paused but still holds its Pandora session.
    await c.post("/api/transport/pause", json={"target": den_side})
    assert rt.store.state.sides[den_side].play_state == "pause"
    ack = (
        await c.post("/api/play", json={"target": HEOS_SIDE, "content_ref": ref("focus-flow")})
    ).json()
    assert ack["ok"] and len(ack["warnings"]) == 1
    assert ack["warnings"][0]["target"] == den_side  # same vendor (HEOS) preferred over Sonos
    assert ack["warnings"][0]["message"] == rt.router.PANDORA_CONCURRENT_MSG


async def test_history_rows_carry_availability_refreshed_from_the_live_map(client) -> None:
    c, rt = client
    await c.post("/api/play", json={"target": HEOS_SIDE, "content_ref": ref("living-room-mix")})
    await c.post(
        "/api/play",
        json={
            "target": HEOS_SIDE,
            "content_ref": {"service": "tidal", "kind": "album", "id": "101"},
        },
    )
    hist = (await c.get("/api/history")).json()["items"]
    by_kind = {h["content_ref"]["kind"]: h for h in hist}
    assert flags(by_kind["station"]["availability"]) == {"heos": True, "sonos": False}
    assert flags(by_kind["album"]["availability"]) == {"heos": True, "sonos": True}
    # Unlink Tidal on Sonos-only? Tidal is linked everywhere at once in fake mode; unlinking it
    # updates the album row's live availability on the next read.
    await c.post("/api/dev/fake/unlink_tidal")
    hist = (await c.get("/api/history")).json()["items"]
    assert flags(next(h for h in hist if h["content_ref"]["kind"] == "album")["availability"]) == {
        "heos": False,
        "sonos": False,
    }
    # Station rows: the snapshot survives when the station map is cold, and follows the map
    # when it is warm (HEOS unlinks Pandora -> Living Room Mix has no vendor left -> snapshot kept).
    await c.post("/api/dev/fake/unlink_pandora?vendor=heos")
    home = (await c.get("/api/home")).json()
    station_recent = next(r for r in home["recents"] if r["content_ref"]["kind"] == "station")
    assert flags(station_recent["availability"]) == {
        "heos": True,
        "sonos": False,
    }  # snapshot: not in map
    await c.post("/api/dev/fake/link_pandora?vendor=heos")
    await c.post("/api/dev/fake/unlink_pandora?vendor=sonos")
    await c.post("/api/play", json={"target": HEOS_SIDE, "content_ref": ref("chill-radio")})
    hist = (await c.get("/api/history")).json()["items"]
    chill = next(h for h in hist if h["content_ref"]["id"] == "chill-radio")
    assert flags(chill["availability"]) == {"heos": True, "sonos": False}
    await c.post("/api/dev/fake/link_pandora?vendor=sonos")
    await c.get("/api/home")  # warms the map with both vendors
    hist = (await c.get("/api/history")).json()["items"]
    chill = next(h for h in hist if h["content_ref"]["id"] == "chill-radio")
    assert flags(chill["availability"]) == {
        "heos": True,
        "sonos": True,
    }  # refreshed from the live map
