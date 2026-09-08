"""Availability reasons on every item, the copy bundle, and the router copy matching it."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from illyhub_hub import messages
from illyhub_hub.api import create_app
from illyhub_hub.content import Availability
from test_phase6_api import HEOS_SIDE, SONOS_SIDE, fake_settings


@pytest.fixture
async def client(tmp_path: Path):
    app = create_app(fake_settings(tmp_path, fake_pandora=True))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c, app.state.runtime


def test_availability_build_orders_unsupported_then_link_then_presence() -> None:
    a = Availability.build("ytmusic", {"heos": True, "sonos": True})
    assert (a.heos, a.sonos) == (False, True)
    assert a.reasons == {"heos": "unsupported", "sonos": None}
    b = Availability.build("tidal", {"heos": False, "sonos": True})
    assert b.reasons == {"heos": "not_linked", "sonos": None}
    c = Availability.build(
        "pandora", {"heos": True, "sonos": True}, present={"heos": False, "sonos": True}
    )
    assert c.reasons == {"heos": "not_in_account", "sonos": None} and c.heos is False
    d = Availability.build(
        "pandora", {"heos": False, "sonos": True}, present={"heos": False, "sonos": True}
    )
    assert d.reasons["heos"] == "not_linked"  # link state beats account presence
    assert Availability().reasons == {"heos": None, "sonos": None}


async def test_reasons_travel_on_browse_items_tracks_and_history(client) -> None:
    c, rt = client
    # unsupported: YouTube Music on HEOS
    yt = (await c.get("/api/browse/ytmusic/albums")).json()["items"][0]
    assert yt["availability"]["reasons"] == {"heos": "unsupported", "sonos": None}
    album = (await c.get(f"/api/browse/ytmusic/album/{yt['content_ref']['id']}")).json()
    assert album["tracks"][0]["availability"]["reasons"]["heos"] == "unsupported"
    # not_in_account: a Pandora station present on one vendor only
    stations = (await c.get("/api/browse/pandora/stations")).json()["items"]
    one_sided = next(s for s in stations if s["title"] == "Living Room Mix")
    assert one_sided["availability"]["reasons"] == {"heos": None, "sonos": "not_in_account"}
    # not_linked: Pandora unlinked in the Sonos app
    await c.post("/api/dev/fake/unlink_pandora?vendor=sonos")
    stations = (await c.get("/api/browse/pandora/stations")).json()["items"]
    assert all(s["availability"]["reasons"]["sonos"] == "not_linked" for s in stations)
    await c.post("/api/dev/fake/link_pandora?vendor=sonos")
    # not_linked: Tidal unlinked everywhere after a play → history refresh carries the reason
    tidal_album = {"service": "tidal", "kind": "album", "id": "101"}
    r = await c.post("/api/play", json={"target": SONOS_SIDE, "content_ref": tidal_album})
    assert r.status_code == 200
    hist = (await c.get("/api/history")).json()["items"]
    assert hist[0]["availability"]["reasons"] == {"heos": None, "sonos": None}
    rt.fakes.set_tidal_linked(False)
    hist = (await c.get("/api/history")).json()["items"]
    assert hist[0]["availability"]["reasons"] == {"heos": "not_linked", "sonos": "not_linked"}
    # the recents rail (home) shows the same reasons
    home = (await c.get("/api/home")).json()
    assert home["recents"][0]["availability"]["reasons"]["heos"] == "not_linked"


async def test_router_copy_matches_the_templates(client) -> None:
    c, rt = client
    ref = {"service": "ytmusic", "kind": "album", "id": "MPREb_alb1"}
    r = await c.post("/api/play", json={"target": HEOS_SIDE, "content_ref": ref})
    assert r.json()["error"]["message"] == messages.not_available_message(
        "unsupported", service="ytmusic", vendor="heos", side="Living Room Amp"
    )
    await c.post("/api/dev/fake/unlink_pandora?vendor=heos")
    station = {"service": "pandora", "kind": "station", "id": "chill-radio"}
    r = await c.post("/api/play", json={"target": HEOS_SIDE, "content_ref": station})
    side_name = rt.store.state.sides[HEOS_SIDE].name
    assert r.json()["error"]["message"] == messages.not_available_message(
        "not_linked", service="pandora", vendor="heos", side=side_name
    )
    await c.post("/api/dev/fake/link_pandora?vendor=heos")
    r = await c.post(
        "/api/play",
        json={"target": SONOS_SIDE, "content_ref": {**station, "id": "living-room-mix"}},
    )
    sonos_name = rt.store.state.sides[SONOS_SIDE].name
    assert r.json()["error"]["message"] == messages.not_available_message(
        "not_in_account", service="pandora", vendor="sonos", side=sonos_name
    )
    assert rt.router.PANDORA_CONCURRENT_MSG == messages.PANDORA_CONCURRENT_MSG
    sync = await c.post(
        "/api/sync/play",
        json={"content_ref": ref, "heos_target": HEOS_SIDE, "sonos_target": SONOS_SIDE},
    )
    assert sync.json()["error"]["message"] == messages.SYNC_UNSUPPORTED_CONTENT


async def test_meta_messages_endpoint_matches_the_cli_bundle(client) -> None:
    c, _rt = client
    served = (await c.get("/api/meta/messages")).json()
    assert served == messages.export()
    assert served["unsupported_on_vendor"] == [["heos", "ytmusic"]]
    assert served["vendor_app"] == {"heos": "HEOS app", "sonos": "Sonos app"}
    assert served["templates"]["pandora_concurrent"] == messages.PANDORA_CONCURRENT_MSG
    out = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "illyhub_hub.messages"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout) == served
    # raw vendor ids never appear in any template
    for tpl in served["templates"]["not_available"].values():
        assert " heos" not in tpl and " sonos" not in tpl
