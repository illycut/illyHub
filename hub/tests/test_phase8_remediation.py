# ruff: noqa: F811 - the `client` fixture is imported from test_phase8_api and named by tests
"""Phase 8 review remediation: fake queue scenarios per vendor with art, Sync Play saving and
restoring shuffle/repeat, and the uptime tick draining a pending metrics rollup off-loop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from illyhub_hub.adapters.fake import HEOS_PLAYER, KITCHEN
from illyhub_hub.metrics import Metrics
from test_phase8_api import ALBUM, HEOS_SIDE, client, wait_for  # noqa: F401 - fixture reuse


async def test_queue_change_scenario_honours_vendor_and_entries_carry_art(client) -> None:
    c, rt = client
    await c.post("/api/play", json={"target": KITCHEN, "content_ref": ALBUM})
    sonos_side = rt.fakes.sonos.side_id()
    before = len(rt.store.state.queues[sonos_side].items)
    r = await c.post("/api/dev/fake/queue_change", params={"vendor": "sonos"})
    assert r.status_code == 200 and r.json()["result"]["side"] == sonos_side
    q = rt.store.state.queues[sonos_side]
    assert len(q.items) == before + 1 and q.items[-1].title == f"Encore {before + 1}"
    assert all(e.art.url for e in q.items)  # the pushed snapshot carries art like a read does
    assert q.source == "tidal"
    # default vendor is HEOS, and its queue is untouched by the Sonos change
    assert HEOS_SIDE not in rt.store.state.queues
    r = await c.post("/api/dev/fake/queue_change")
    assert r.json()["result"]["queued"] == 0  # HEOS has nothing queued yet
    r = await c.post("/api/dev/fake/queue_change", params={"vendor": "denon"})
    assert r.status_code == 400


async def test_sync_play_saves_and_restores_play_modes(client) -> None:
    c, rt = client
    sonos_side = rt.fakes.sonos.side_id()
    await c.post("/api/playmode", json={"target": HEOS_PLAYER, "shuffle": True, "repeat": "all"})
    await c.post("/api/playmode", json={"target": KITCHEN, "repeat": "one"})
    body = {"content_ref": ALBUM, "heos_target": HEOS_PLAYER, "sonos_target": KITCHEN}
    r = await c.post("/api/sync/play", json=body)
    assert r.status_code == 200, r.text
    await wait_for(lambda: rt.store.state.sync.status == "locked")
    assert rt.store.state.sides[HEOS_SIDE].play_mode.model_dump() == {
        "shuffle": False,
        "repeat": "off",
    }
    assert rt.store.state.sides[sonos_side].play_mode.model_dump() == {
        "shuffle": False,
        "repeat": "off",
    }
    await c.post("/api/sync/stop")
    await wait_for(
        lambda: (
            rt.store.state.sides[HEOS_SIDE].play_mode.shuffle is True
            and rt.store.state.sides[sonos_side].play_mode.repeat == "one"
        )
    )
    assert rt.store.state.sides[HEOS_SIDE].play_mode.repeat == "all"


async def test_uptime_tick_drains_a_pending_rollup(client) -> None:
    c, rt = client
    assert rt.metrics is not None
    m: Metrics = rt.metrics
    # Move the metrics clock across midnight so the next tick closes the day off-loop.
    fixed = datetime(2026, 9, 9, 0, 0, 30, tzinfo=UTC)
    m._day = (fixed - timedelta(days=1)).date()  # noqa: SLF001 - simulate "yesterday still open"
    m._clock = lambda: fixed  # noqa: SLF001
    m.record_command("play", True, 12.0, ["heos"])  # lands in the (now finished) day buffer
    await asyncio.sleep(0.2)  # a few metrics_tick_s=0.05 ticks: roll + drain
    r = await c.get("/api/metrics")
    days = r.json()["days"]
    assert days and days[0]["day"] == "2026-09-08" and r.json()["today"]["day"] == "2026-09-09"
    assert m._pending_rollup is None  # noqa: SLF001
