"""Phase 4: Sync Play engine against the fakes (independent clocks, seek latency, per-vendor
track-id namespaces, 0.5-confidence first reports) and its API."""

from __future__ import annotations

import asyncio
import csv
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from illyhub_hub.adapters.base import PrimedQueue, UnsupportedCommandError
from illyhub_hub.adapters.fake import (
    HEOS_PLAYER,
    HEOS_PLAYER_2,
    KITCHEN,
    PATIO,
    FakeHeos,
    FakeSonos,
    FakeVendorMixin,
)
from illyhub_hub.api import HubRuntime, create_app
from illyhub_hub.commands import Ack, CommandError, CommandRouter, LatencyTracker
from illyhub_hub.config import Settings
from illyhub_hub.content import BrowseItem, ContentRef
from illyhub_hub.state import NowPlaying, Position, Side, StateStore
from illyhub_hub.sync import (
    DriftLog,
    SyncConfig,
    SyncEngine,
    SyncSession,
    canonical_from_vendor_id,
    primed_matches,
    report_from_samples,
    track_index,
    trim_sessions,
)

ALBUM = {"service": "tidal", "kind": "album", "id": "101"}
HEOS_SIDE = "heos:heos-1"


def sync_settings(tmp_path: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = dict(
        fake_devices=True,
        fake_tidal=True,
        position_poll_s=0.02,
        command_coalesce_s=0.0,
        sync_monitor_s=0.02,
        sync_correction_gap_s=0.3,
        sync_track_grace_s=0.15,
        sync_lookahead_ms=0,
        sync_drift_ms=100,
        data_dir=str(tmp_path / "data"),
        app_dir=str(tmp_path / "no-app"),
        fonts_dir=str(tmp_path / "no-fonts"),
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


class SeekSpy:
    """Counts seeks per vendor without changing behaviour (HEOS still refuses)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: dict[str, int] = {"heos": 0, "sonos": 0}
        self.targets: list[int] = []
        heos_seek, sonos_seek = FakeHeos.seek, FakeSonos.seek

        async def heos(self_: FakeHeos, pid: str, ms: int) -> None:
            self.calls["heos"] += 1
            await heos_seek(self_, pid, ms)

        async def sonos(self_: FakeSonos, pid: str, ms: int) -> None:
            self.calls["sonos"] += 1
            self.targets.append(ms)
            await sonos_seek(self_, pid, ms)

        monkeypatch.setattr(FakeHeos, "seek", heos)
        monkeypatch.setattr(FakeSonos, "seek", sonos)


Rig = tuple[httpx.AsyncClient, HubRuntime, SeekSpy]


async def make_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any):
    spy = SeekSpy(monkeypatch)
    app = create_app(sync_settings(tmp_path, **kw))
    return app, spy


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    app, spy = await make_rig(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c, app.state.runtime, spy


async def wait_for(pred: Callable[[], Any], timeout: float = 3.0, step: float = 0.02) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(step)


async def start_sync(c: httpx.AsyncClient, rt: HubRuntime, **extra: Any) -> dict[str, Any]:
    body = {"content_ref": ALBUM, "heos_target": HEOS_PLAYER, "sonos_target": KITCHEN, **extra}
    r = await c.post("/api/sync/play", json=body)
    assert r.status_code == 200, r.text
    ack = r.json()
    assert ack["ok"] and ack["action"] == "sync_play" and ack["session_id"]
    assert rt.store.state.sync.status in ("starting", "locked")  # the ack returns at starting
    await wait_for(lambda: rt.store.state.sync.status == "locked", timeout=3.0)
    return ack


def sonos_side(rt: HubRuntime) -> str:
    return rt.fakes.sonos.side_id()


def sonos_name(rt: HubRuntime) -> str:
    return rt.store.state.sides[sonos_side(rt)].name  # "Kitchen + Patio" (grouped fake)


# ---------------------------------------------------------------------------------------
# start, identity, lock
# ---------------------------------------------------------------------------------------


async def test_sync_play_primes_verifies_starts_and_locks(rig: Rig) -> None:
    c, rt, spy = rig
    ack = await start_sync(c, rt)
    follower = sonos_side(rt)
    assert ack["applied"] == [HEOS_SIDE, follower]
    st = rt.store.state
    assert st.sync.session_id == ack["session_id"]
    assert st.sync.master_side == HEOS_SIDE and st.sync.follower_side == follower
    assert st.sync.title == "Warm Glow" and st.sync.content_ref.model_dump() == ALBUM
    assert st.sides[HEOS_SIDE].play_state == st.sides[follower].play_state == "play"
    # Finding 1: vendor ids live in different namespaces; the canonical identity matches.
    np_m, np_f = st.now_playing[HEOS_SIDE], st.now_playing[follower]
    assert np_m.track_id != np_f.track_id
    assert np_f.track_id.startswith("x-sonos-http:track/") and np_m.track_id.isdigit()
    assert np_m.content_ref == np_f.content_ref and np_m.content_ref.id == np_m.track_id
    # start delta lands from the background measurement (finding 13)
    await wait_for(lambda: rt.store.state.sync.start_delta_ms is not None, timeout=2.0)
    assert abs(rt.store.state.sync.start_delta_ms) < 200
    hist = (await c.get("/api/history")).json()["items"]
    assert (
        hist and hist[0]["sync"] is True and set(hist[0]["last_targets"]) == {HEOS_SIDE, follower}
    )
    await asyncio.sleep(0.2)
    assert rt.store.state.sync.status == "locked" and abs(rt.store.state.sync.drift_ms or 0) < 100
    assert spy.calls == {"heos": 0, "sonos": 0}
    assert (await c.get("/api/sync")).json()["status"] == "locked"


def test_identity_helpers_never_compare_across_vendors() -> None:
    assert canonical_from_vendor_id("heos", "10101") == "10101"
    assert canonical_from_vendor_id("heos", "spotify:track:x") is None
    assert (
        canonical_from_vendor_id("sonos", "x-sonos-http:track/10101.flac?sid=174&sn=1") == "10101"
    )
    assert canonical_from_vendor_id("sonos", "x-rincon-mp3radio://…") is None
    assert canonical_from_vendor_id("sonos", None) is None
    t = PlayableTrackFactory("10101", "Signal", "Analog Heart")
    assert primed_matches("heos", PrimedQueue(track_id="10101", title="whatever"), t)
    assert not primed_matches("heos", PrimedQueue(track_id="10102", title="Signal"), t)
    assert primed_matches("sonos", PrimedQueue(track_id="x-sonos-http:track/10101.flac?sid=174"), t)
    assert not primed_matches("sonos", PrimedQueue(track_id="x-sonos-http:track/10102.flac"), t)
    assert primed_matches("sonos", PrimedQueue(track_id="x-rincon-queue:foo", title=" signal "), t)
    # a raw Sonos URI is never compared against a HEOS id even when the digits happen to match
    tracks = [
        PlayableTrackFactory("1", "Dup", "A"),
        PlayableTrackFactory("2", "Other", "A"),
        PlayableTrackFactory("1", "Dup", "A"),
    ]
    ref = ContentRef(service="tidal", kind="track", id="1")
    assert (
        track_index(NowPlaying(title="Dup", content_ref=ref), tracks, near=2, service="tidal") == 2
    )
    assert (
        track_index(NowPlaying(title="Dup", content_ref=ref), tracks, near=0, service="tidal") == 0
    )
    assert track_index(NowPlaying(title="Other", artist="A"), tracks, near=0, service="tidal") == 1
    assert (
        track_index(NowPlaying(title="Other", artist="B"), tracks, near=0, service="tidal") is None
    )
    assert track_index(None, tracks, near=0, service="tidal") is None


def PlayableTrackFactory(track_id: str, title: str, artist: str):  # noqa: N802 - test helper
    from illyhub_hub.adapters.base import PlayableTrack

    return PlayableTrack(service="tidal", track_id=track_id, title=title, artist=artist)


def _session(engine: SyncEngine, tracks: list[Any] | None = None) -> SyncSession:
    heos = Side(id="heos:h", vendor="heos", coordinator_player_id="h", member_ids=["h"], name="H")
    sonos = Side(
        id="sonos:s", vendor="sonos", coordinator_player_id="s", member_ids=["s"], name="S"
    )
    ref = ContentRef(service="tidal", kind="album", id="1")
    return SyncSession(
        id="t",
        generation=1,
        ref=ref,
        item=BrowseItem(content_ref=ref, title="x"),
        tracks=tracks or [PlayableTrackFactory("10101", "Signal", "A")],
        index=0,
        master=heos,
        follower=sonos,
        log=DriftLog(Path("/tmp/illyhub-unused"), "t"),
        started_at=datetime.now(UTC),
    )


def test_verify_checks_each_side_against_its_own_expectation() -> None:
    store = StateStore()
    engine = SyncEngine(store, CommandRouter(store), tracks_for=None, log_dir=Path("/tmp/x"))  # type: ignore[arg-type]
    session = _session(engine)
    session.expected = {
        "heos:h": PrimedQueue(track_id="10101", title="Signal"),
        "sonos:s": PrimedQueue(
            track_id="x-sonos-http:track/10101.flac?sid=174&sn=1", title="Signal"
        ),
    }
    engine._verify(session)  # different vendor ids, same canonical track: fine
    session.expected["sonos:s"] = PrimedQueue(
        track_id="x-sonos-http:track/10102.flac", title="Signal"
    )
    with pytest.raises(CommandError) as exc:
        engine._verify(session)
    assert exc.value.code == "sync_mismatch" and "S" in exc.value.message
    session.expected["sonos:s"] = PrimedQueue(title="Signal")  # no id: title decides
    engine._verify(session)
    session.expected.pop("heos:h")
    with pytest.raises(CommandError):
        engine._verify(session)


# ---------------------------------------------------------------------------------------
# drift, corrections, settle, confidence, time-box
# ---------------------------------------------------------------------------------------


async def test_drift_is_corrected_on_the_follower_only(rig: Rig) -> None:
    c, rt, spy = rig
    await start_sync(c, rt)
    follower = sonos_side(rt)
    rt.fakes.ticker.set_rate(follower, 1.3)  # Sonos runs 30% fast: 6 ms per 20 ms tick
    await wait_for(lambda: rt.store.state.sync.corrections >= 1, timeout=3.0)
    assert spy.calls["sonos"] >= 1 and spy.calls["heos"] == 0
    await wait_for(lambda: rt.store.state.sync.status == "locked", timeout=1.5)
    assert rt.store.state.sync.last_correction_at is not None
    await c.post("/api/sync/stop")
    report = (await c.get(f"/api/sync/sessions/{rt.store.state.sync.session_id}/report")).json()
    assert report["corrections"] >= 1 and report["samples"] > 0 and report["drift_max_ms"] >= 100


async def test_drift_correction_relocks_with_default_lookahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default lookahead (400 ms) with instant fake seeks: the first landing reveals latency 0 and
    the one settle correction removes the overshoot: drifting → correcting → locked."""
    app, spy = await make_rig(
        tmp_path, monkeypatch, sync_lookahead_ms=400, sync_correction_gap_s=10.0
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as c:
            rt = app.state.runtime
            await start_sync(c, rt)
            await c.post("/api/dev/fake/sync_drift")
            await wait_for(lambda: rt.store.state.sync.corrections >= 1, timeout=2.0)
            await wait_for(
                lambda: (
                    rt.store.state.sync.status == "locked"
                    and abs(rt.store.state.sync.drift_ms or 0) < 100
                ),
                timeout=2.0,
            )
            assert rt.store.state.sync.corrections == 2  # the correction plus one settle
            assert rt.sync.session is not None and rt.sync.session.lookahead_ms < 60
            await asyncio.sleep(0.2)
            assert rt.store.state.sync.status == "locked" and spy.calls["heos"] == 0


async def test_settle_correction_is_armed_once_per_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 3: persistent drift at 6x with a 10 s gap → the initial correction and at most one
    settle in the first second; the gap is never bypassed twice in a row."""
    app, spy = await make_rig(tmp_path, monkeypatch, sync_correction_gap_s=10.0)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as c:
            rt = app.state.runtime
            await start_sync(c, rt)
            rt.fakes.ticker.set_rate(sonos_side(rt), 6.0)  # 100 ms of drift per 20 ms tick
            await asyncio.sleep(1.0)
            assert 1 <= rt.store.state.sync.corrections <= 2
            assert spy.calls["sonos"] == rt.store.state.sync.corrections
            session = rt.sync.session
            assert session is not None and session.settle_used or not session.settle_armed


async def test_unconfirmed_reports_are_not_judged(rig: Rig) -> None:
    """Finding 7: samples where either side's confidence is 0.5 are skipped and not logged."""
    c, rt, spy = rig
    await start_sync(c, rt)
    follower = sonos_side(rt)
    await rt.fakes.ticker.stop()  # freeze the fakes; feed positions by hand
    await asyncio.sleep(0.05)
    samples_before = len(rt.sync.session.log.samples)
    for _ in range(5):
        rt.store.set_position(HEOS_SIDE, Position(position_ms=1000, confidence=1.0))
        rt.store.set_position(follower, Position(position_ms=6000, confidence=0.5))
        await asyncio.sleep(0.05)
    assert rt.store.state.sync.corrections == 0 and spy.calls["sonos"] == 0
    assert len(rt.sync.session.log.samples) == samples_before
    rt.store.set_position(follower, Position(position_ms=6000, confidence=1.0))
    await wait_for(lambda: rt.store.state.sync.corrections == 1, timeout=1.0)


async def test_unconfirmed_correction_is_time_boxed(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """Finding 8: a seek that is never confirmed stops blocking judgement after 2 x gap and leaves
    the lookahead alone."""
    c, rt, _spy = rig
    await start_sync(c, rt)
    follower = sonos_side(rt)
    await rt.fakes.ticker.stop()  # no device reports from here on
    rt.store.set_position(HEOS_SIDE, Position(position_ms=1000, confidence=1.0))
    for _ in range(3):
        rt.store.set_position(follower, Position(position_ms=3000, confidence=1.0))
        await asyncio.sleep(0.04)
    await wait_for(lambda: rt.store.state.sync.corrections >= 1, timeout=1.0)
    session = rt.sync.session
    assert session is not None
    # the only follower report after this seek is the fake's optimistic 0.5 one: never confirmed
    await wait_for(lambda: session.seek_at is not None, timeout=1.5)
    pending = session.seek_at
    before = session.lookahead_ms
    assert rt.store.state.sync.status == "correcting"
    caplog.clear()
    await wait_for(lambda: session.seek_at != pending, timeout=1.5)  # 2 x 0.3 s gap
    assert "never confirmed" in caplog.text
    assert session.lookahead_ms == before  # no latency update from a time-boxed seek


async def test_follower_seek_failure_mid_correction_loses(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, rt, _spy = rig
    await start_sync(c, rt)

    async def broken(self_, pid, ms):
        raise ConnectionError("sonos went away")

    monkeypatch.setattr(FakeSonos, "seek", broken)
    await c.post("/api/dev/fake/sync_drift")
    await wait_for(lambda: rt.store.state.sync.status == "lost", timeout=2.0)
    assert "Kitchen" in (rt.store.state.sync.reason or "")


async def test_missing_positions_skip_the_sample(rig: Rig) -> None:
    c, rt, _spy = rig
    await start_sync(c, rt)
    await rt.fakes.ticker.stop()
    rt.store._mutate(lambda s: s.positions.clear())  # noqa: SLF001 - simulate no reports yet
    await asyncio.sleep(0.2)
    assert rt.store.state.sync.status == "locked"
    assert rt.sync._position(rt.store.state.sides[HEOS_SIDE], datetime.now(UTC)) is None


# ---------------------------------------------------------------------------------------
# boundaries
# ---------------------------------------------------------------------------------------


async def test_master_track_change_reprimes_the_follower(rig: Rig) -> None:
    c, rt, spy = rig
    await start_sync(c, rt)
    follower = sonos_side(rt)
    r = await c.post("/api/dev/fake/sync_track_change")
    assert r.json()["result"]["advanced"] is True
    await wait_for(
        lambda: (
            rt.store.state.now_playing[follower].content_ref
            == rt.store.state.now_playing[HEOS_SIDE].content_ref
        ),
        timeout=2.0,
    )
    await wait_for(lambda: rt.store.state.sync.status == "locked", timeout=2.0)
    assert rt.fakes.sonos.queue_index(KITCHEN) == rt.fakes.heos.queue_index(HEOS_PLAYER) == 1
    assert rt.sync.session is not None and rt.sync.session.reprimes == 1
    assert spy.calls["heos"] == 0


async def test_follower_crossing_first_waits_and_never_seeks_backwards(rig: Rig) -> None:
    """Finding 2: the follower reaches index + 1 before the master. No seek, no re-prime within
    the grace; when the master follows, both settle on the new track."""
    c, rt, spy = rig
    await start_sync(c, rt)
    follower = sonos_side(rt)
    rt.fakes.sonos._advance_queue(KITCHEN, +1)  # noqa: SLF001 - simulate the gapless advance
    await asyncio.sleep(0.08)  # inside the 0.15 s grace
    assert spy.calls["sonos"] == 0 and rt.sync.session.reprimes == 0
    assert rt.store.state.sync.status not in ("lost", "stopped")
    assert rt.sync.session.boundary_kind == "follower_first"
    rt.fakes.heos._advance_queue(HEOS_PLAYER, +1)  # noqa: SLF001 - the master follows
    await wait_for(lambda: rt.sync.session.index == 1 and rt.sync.session.boundary_kind is None)
    await wait_for(lambda: rt.store.state.sync.status == "locked", timeout=2.0)
    assert spy.calls["sonos"] == 0 and rt.sync.session.reprimes == 0
    assert rt.store.state.now_playing[follower].content_ref.id == "10102"


async def test_follower_crossing_first_without_master_is_reprimed_forward_not_seeked(
    rig: Rig,
) -> None:
    c, rt, spy = rig
    await start_sync(c, rt)
    rt.fakes.sonos._advance_queue(KITCHEN, +1)  # noqa: SLF001
    await wait_for(lambda: rt.sync.session.reprimes == 1, timeout=2.0)
    seeks_at_reprime = spy.calls["sonos"]
    assert seeks_at_reprime == 0  # while on index + 1 the follower was never seeked backwards
    assert rt.fakes.sonos.queue_index(KITCHEN) == 0  # re-primed at the master's track instead
    await wait_for(lambda: rt.store.state.sync.status == "locked", timeout=2.0)
    # any seek after the re-prime is the forward catch-up on the same track as the master
    assert all(t >= 0 for t in spy.targets)
    assert rt.store.state.now_playing[sonos_side(rt)].content_ref.id == "10101"


async def test_reprime_failure_loses(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    c, rt, _spy = rig
    await start_sync(c, rt)

    async def broken(self_, *a, **k):
        raise ConnectionError("queue rejected")

    monkeypatch.setattr(FakeSonos, "prime_content", broken)
    await c.post("/api/dev/fake/sync_track_change")
    await wait_for(lambda: rt.store.state.sync.status == "lost", timeout=2.0)
    assert f"{sonos_name(rt)} didn't prime" in (rt.store.state.sync.reason or "")


async def test_master_track_change_during_in_flight_correction(rig: Rig) -> None:
    c, rt, _spy = rig
    await start_sync(c, rt)
    await c.post("/api/dev/fake/sync_drift")
    await wait_for(lambda: rt.sync.session.seek_at is not None, timeout=2.0)
    await c.post("/api/dev/fake/sync_track_change")
    await wait_for(lambda: rt.sync.session.index == 1, timeout=2.0)
    await wait_for(lambda: rt.store.state.sync.status == "locked", timeout=2.0)
    assert rt.sync.session.seek_at is None


async def test_end_of_content_stops_with_playback_ended(rig: Rig) -> None:
    c, rt, _spy = rig
    tracks = (await c.get("/api/browse/tidal/album/101")).json()["tracks"]
    await start_sync(c, rt, start_index=len(tracks) - 1)
    follower = sonos_side(rt)
    for side in (HEOS_SIDE, follower):
        np = rt.store.state.now_playing[side]
        rt.store.set_now_playing(side, np.model_copy(update={"duration_ms": 400}))
    await wait_for(lambda: rt.store.state.sync.status == "stopped", timeout=8.0)
    assert rt.store.state.sync.reason == "playback ended"


# ---------------------------------------------------------------------------------------
# stop / retry / supersede / lost
# ---------------------------------------------------------------------------------------


async def test_scenario_drift_then_stop_keeps_both_playing(rig: Rig) -> None:
    c, rt, _spy = rig
    await start_sync(c, rt)
    r = await c.post("/api/dev/fake/sync_drift")
    assert r.json()["result"]["nudged_ms"] == 800
    await wait_for(lambda: rt.store.state.sync.corrections >= 1, timeout=1.5)
    r = await c.post("/api/sync/stop")
    assert r.status_code == 200 and r.json()["action"] == "sync_stop"
    st = rt.store.state
    assert st.sync.status == "stopped" and st.sync.reason == "stopped by the user"
    assert st.sides[HEOS_SIDE].play_state == st.sides[sonos_side(rt)].play_state == "play"
    rows = list(csv.reader((rt.settings.sync_log_path / f"{st.sync.session_id}.csv").open()))
    assert rows[0] == ["t", "master_pos", "follower_pos", "drift", "action"]
    assert {"correct", "stop"} <= {r[4] for r in rows[1:]}
    r = await c.post("/api/sync/stop")
    assert r.status_code == 409 and r.json()["error"]["code"] == "sync_idle"
    # Finding 6: a stopped session is not retried
    r = await c.post("/api/sync/retry")
    assert r.status_code == 409 and r.json()["error"]["code"] == "sync_stopped"


async def test_external_pause_loses_sync_and_retry_recovers(rig: Rig) -> None:
    c, rt, spy = rig
    await start_sync(c, rt)
    r = await c.post("/api/sync/retry")  # running: nothing to retry
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_argument"
    await asyncio.sleep(0.1)
    r = await c.post("/api/dev/fake/sync_lose_sonos")
    assert r.json()["result"]["play_state"] == "pause"
    await wait_for(lambda: rt.store.state.sync.status == "lost", timeout=8.0)
    assert "Kitchen" in (rt.store.state.sync.reason or "")
    assert rt.store.state.sides[HEOS_SIDE].play_state == "play"  # master untouched
    r = await c.post("/api/sync/retry")
    assert r.status_code == 200 and r.json()["action"] == "sync_retry"
    await wait_for(lambda: rt.store.state.sync.status == "locked", timeout=3.0)
    assert rt.store.state.sides[sonos_side(rt)].play_state == "play" and spy.calls["heos"] == 0


async def test_retry_while_master_stopped_restarts_both(rig: Rig) -> None:
    c, rt, _spy = rig
    await start_sync(c, rt)
    await asyncio.sleep(0.05)
    rt.fakes.heos.sim_play_state(HEOS_PLAYER, "stop")  # from the vendor app
    await wait_for(lambda: rt.store.state.sync.status == "lost", timeout=8.0)
    assert "Living Room Amp" in (rt.store.state.sync.reason or "")
    r = await c.post("/api/sync/retry")
    assert r.status_code == 200
    await wait_for(lambda: rt.store.state.sync.status == "locked", timeout=3.0)
    assert rt.store.state.sides[HEOS_SIDE].play_state == "play"


async def test_adapter_drop_loses_sync(rig: Rig) -> None:
    c, rt, _spy = rig
    await start_sync(c, rt)
    await c.post("/api/dev/fake/disconnect_heos")
    await wait_for(lambda: rt.store.state.sync.status == "lost", timeout=1.0)
    assert "heos link" in (rt.store.state.sync.reason or "")
    await c.post("/api/dev/fake/reconnect_heos")
    r = await c.post("/api/sync/retry")
    assert r.status_code == 200
    await wait_for(lambda: rt.store.state.sync.status == "locked", timeout=3.0)


async def test_group_first_when_several_players_per_vendor(rig: Rig) -> None:
    c, rt, _spy = rig
    ack = await start_sync(
        c, rt, heos_target=[HEOS_PLAYER, HEOS_PLAYER_2], sonos_target=[KITCHEN, PATIO]
    )
    master, follower = ack["applied"]
    assert master.startswith("heos:heos-g") and follower.startswith("sonos:sonos-g")
    assert set(rt.store.state.sides[master].member_ids) == {HEOS_PLAYER, HEOS_PLAYER_2}


async def test_resolve_failure_undoes_groups_it_created(rig: Rig) -> None:
    """Nit 24: a HEOS group made for a session that never starts is dissolved again."""
    c, rt, _spy = rig
    rt.fakes.sonos.linked_services.discard("tidal")
    r = await c.post(
        "/api/sync/play",
        json={
            "content_ref": ALBUM,
            "heos_target": [HEOS_PLAYER, HEOS_PLAYER_2],
            "sonos_target": KITCHEN,
        },
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_available_on_side"
    assert not any(g.vendor == "heos" for g in rt.store.state.groups.values())
    assert rt.store.state.sync.status == "idle"


async def test_new_session_supersedes_the_old_one_and_trims_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _spy = await make_rig(tmp_path, monkeypatch, sync_max_sessions=2)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as c:
            rt = app.state.runtime
            ids = [(await start_sync(c, rt, start_index=i))["session_id"] for i in range(3)]
            assert len(set(ids)) == 3 and rt.store.state.sync.session_id == ids[2]
            listed = {
                s["id"]: s["status"] for s in (await c.get("/api/sync/sessions")).json()["sessions"]
            }
            assert listed == {
                ids[0]: "stopped",
                ids[1]: "stopped",
            }  # the live one is not on disk yet
            await c.post("/api/sync/stop")
            listed = {
                s["id"]: s["status"] for s in (await c.get("/api/sync/sessions")).json()["sessions"]
            }
            assert listed == {
                ids[1]: "stopped",
                ids[2]: "stopped",
            }  # retention: newest 2 (finding 16)
            assert len((await c.get("/api/sync/sessions?limit=1")).json()["sessions"]) == 1
            assert (await c.get("/api/sync/sessions/nope/report")).status_code == 404


async def test_concurrent_plays_yield_one_session_and_one_monitor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 5: two plays racing through priming: distinct ids, the first is superseded before
    it fires, one monitor, start_primed fired exactly twice (once per side, for the winner)."""
    fired: list[str] = []
    orig_prime, orig_start = FakeVendorMixin.prime_content, FakeVendorMixin.start_primed

    async def slow_prime(self_, *a, **k):
        await asyncio.sleep(0.25)
        return await orig_prime(self_, *a, **k)

    async def counting_start(self_, pid, idx=0):
        fired.append(self_.vendor)
        await orig_start(self_, pid, idx)

    monkeypatch.setattr(FakeVendorMixin, "prime_content", slow_prime)
    monkeypatch.setattr(FakeVendorMixin, "start_primed", counting_start)
    app, _spy = await make_rig(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as c:
            rt = app.state.runtime
            body = {"content_ref": ALBUM, "heos_target": HEOS_PLAYER, "sonos_target": KITCHEN}
            a = asyncio.create_task(c.post("/api/sync/play", json=body))
            await asyncio.sleep(0.05)  # A is priming
            b = asyncio.create_task(c.post("/api/sync/play", json={**body, "start_index": 1}))
            ra, rb = await asyncio.gather(a, b)
            assert ra.json()["ok"] is False and ra.json()["error"]["code"] == "sync_stopped"
            assert rb.json()["ok"] is True
            assert rt.store.state.sync.session_id == rb.json()["session_id"]
            await wait_for(lambda: rt.store.state.sync.status == "locked", timeout=3.0)
            assert sorted(fired) == ["heos", "sonos"]
            live = [
                t
                for t in rt.sync._tasks
                if t.get_coro().__name__ == "_monitor_loop" and not t.done()
            ]  # noqa: SLF001
            assert len(live) == 1


async def test_prime_failure_after_the_other_side_primed_restores_it(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 12: parallel prime; HEOS fails after Sonos rebuilt its queue → the message says the
    Sonos queue was restored (SoCo Snapshot in the fake) and nothing starts."""
    c, rt, _spy = rig
    orig = FakeVendorMixin.prime_content

    async def heos_fails(self_, *a, **k):
        if self_.vendor == "heos":
            await asyncio.sleep(0.05)
            raise ConnectionError("HEOS refused the container")
        return await orig(self_, *a, **k)

    monkeypatch.setattr(FakeVendorMixin, "prime_content", heos_fails)
    r = await c.post(
        "/api/sync/play",
        json={"content_ref": ALBUM, "heos_target": HEOS_PLAYER, "sonos_target": KITCHEN},
    )
    assert r.status_code == 502
    msg = r.json()["error"]["message"]
    assert "Living Room Amp didn't prime" in msg and f"{sonos_name(rt)}'s queue was restored" in msg
    assert rt.fakes.sonos.restored_queues == 1
    assert rt.store.state.sync.status == "lost"


async def test_refusals(rig: Rig) -> None:
    c, rt, _spy = rig
    r = await c.post(
        "/api/sync/play",
        json={
            "content_ref": {"service": "pandora", "kind": "station", "id": "s1"},
            "heos_target": HEOS_PLAYER,
            "sonos_target": KITCHEN,
        },
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "unsupported_content"
    assert rt.store.state.sync.status == "idle"
    for heos_target in (KITCHEN, "all", []):
        r = await c.post(
            "/api/sync/play",
            json={"content_ref": ALBUM, "heos_target": heos_target, "sonos_target": KITCHEN},
        )
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_argument"
    r = await c.post("/api/sync/retry")
    assert r.status_code == 409 and r.json()["error"]["code"] == "sync_idle"
    await c.post("/api/dev/fake/unlink_tidal")
    r = await c.post(
        "/api/sync/play",
        json={"content_ref": ALBUM, "heos_target": HEOS_PLAYER, "sonos_target": KITCHEN},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "needs_link"
    assert rt.store.state.sides[HEOS_SIDE].play_state != "play"
    await c.post("/api/dev/fake/link_tidal")
    rt.fakes.sonos.linked_services.discard("tidal")
    r = await c.post(
        "/api/sync/play",
        json={"content_ref": ALBUM, "heos_target": HEOS_PLAYER, "sonos_target": KITCHEN},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_available_on_side"
    assert "Kitchen" in r.json()["error"]["message"]


@pytest.mark.parametrize("phase", ["resolving", "priming", "verifying", "starting"])
async def test_stop_works_in_every_start_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    from illyhub_hub.services.tidal import TidalService

    async def slow(*a: Any, **k: Any) -> Any:
        await asyncio.sleep(5)

    if phase == "resolving":
        orig = TidalService.tracks_for

        async def slow_tracks(self_, ref):
            await asyncio.sleep(5)
            return await orig(self_, ref)

        monkeypatch.setattr(TidalService, "tracks_for", slow_tracks)
    elif phase == "priming":
        monkeypatch.setattr(FakeVendorMixin, "prime_content", slow)
    elif phase == "verifying":
        orig_verify = SyncEngine._verify

        def slow_verify(self_, session):
            orig_verify(self_, session)
            self_._publish(session, "verifying")
            raise _HangError()

        monkeypatch.setattr(SyncEngine, "_verify", slow_verify)
        orig_pas = SyncEngine._prime_and_start

        async def prime_and_start(self_, session, *, both):
            try:
                await orig_pas(self_, session, both=both)
            except _HangError:
                await asyncio.sleep(5)

        monkeypatch.setattr(SyncEngine, "_prime_and_start", prime_and_start)
    else:  # starting
        monkeypatch.setattr(FakeVendorMixin, "start_primed", slow)

    app, _spy = await make_rig(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as c:
            rt = app.state.runtime
            body = {"content_ref": ALBUM, "heos_target": HEOS_PLAYER, "sonos_target": KITCHEN}
            play = asyncio.create_task(c.post("/api/sync/play", json=body))
            await wait_for(lambda: rt.store.state.sync.status == phase, timeout=3.0)
            if phase == "resolving":
                assert rt.store.state.sync.session_id is None  # nit 19: fresh state, no stale id
            r = await c.post("/api/sync/stop")
            assert r.status_code == 200, r.text
            st = rt.store.state.sync
            assert st.status == "stopped" and st.reason == "stopped by the user"
            ack = (await play).json()
            assert ack["ok"] is False and ack["error"]["code"] == "sync_stopped"
            assert rt.sync._start_task is None  # noqa: SLF001
            monkeypatch.undo()
            await asyncio.sleep(0.05)
            r = await c.post("/api/sync/play", json=body)
            assert r.status_code == 200
            await wait_for(lambda: rt.store.state.sync.status == "locked", timeout=3.0)


class _HangError(Exception):
    pass


# ---------------------------------------------------------------------------------------
# transport mirroring (findings 9, 10, 15, nit 25)
# ---------------------------------------------------------------------------------------


async def test_transport_on_the_master_is_mirrored_in_order(rig: Rig) -> None:
    c, rt, spy = rig
    await start_sync(c, rt)
    follower = sonos_side(rt)
    # pause through the hub: follower pauses too; the session is paused, not lost
    r = await c.post("/api/transport/pause", json={"target": HEOS_PLAYER})
    assert r.status_code == 200 and r.json()["resolved"] == {HEOS_SIDE: "pause"}
    await wait_for(lambda: rt.store.state.sides[follower].play_state == "pause", timeout=1.0)
    await asyncio.sleep(0.3)
    assert rt.store.state.sync.status not in ("lost", "stopped") and rt.sync.session.paused
    # pause, play back to back stay ordered: ends playing
    await c.post("/api/transport/pause", json={"target": HEOS_SIDE})
    await c.post("/api/transport/play", json={"target": HEOS_SIDE})
    await wait_for(lambda: rt.store.state.sides[follower].play_state == "play", timeout=1.0)
    await rt.sync._mirror_queue.join()  # noqa: SLF001
    assert rt.store.state.sides[follower].play_state == "play" and not rt.sync.session.paused
    await wait_for(lambda: rt.store.state.sync.status == "locked", timeout=2.0)
    # next, next back to back: the follower lands on index 2 exactly once each
    await c.post("/api/transport/next", json={"target": HEOS_PLAYER})
    await c.post("/api/transport/next", json={"target": HEOS_PLAYER})
    await rt.sync._mirror_queue.join()  # noqa: SLF001
    await wait_for(
        lambda: rt.fakes.sonos.queue_index(KITCHEN) == rt.fakes.heos.queue_index(HEOS_PLAYER) == 2,
        timeout=1.0,
    )
    await wait_for(lambda: rt.store.state.sync.status == "locked", timeout=2.0)
    # toggle mirrors the action the router actually sent (finding 10)
    r = await c.post("/api/transport/toggle", json={"target": HEOS_PLAYER})
    assert r.json()["resolved"] == {HEOS_SIDE: "pause"}
    await wait_for(lambda: rt.store.state.sides[follower].play_state == "pause", timeout=1.0)
    # "all" reaches both directly; the engine only updates its bookkeeping
    await c.post("/api/transport/play", json={"target": "all"})
    await wait_for(lambda: rt.store.state.sync.status == "locked", timeout=2.0)
    assert not rt.sync.session.paused
    # stop on the master ends the session as stopped and stops the follower
    await c.post("/api/transport/stop", json={"target": HEOS_PLAYER})
    await wait_for(lambda: rt.store.state.sync.status == "stopped", timeout=1.0)
    assert "Living Room Amp" in (rt.store.state.sync.reason or "")
    assert rt.store.state.sides[follower].play_state == "stop" and spy.calls["heos"] == 0
    # no mirroring after the session ended
    rt.fakes.sonos.sim_play_state(KITCHEN, "play")
    await c.post("/api/transport/pause", json={"target": HEOS_PLAYER})
    await asyncio.sleep(0.1)
    assert rt.store.state.sides[follower].play_state == "play"


def test_toggle_mirroring_uses_the_resolved_action_not_post_ack_state() -> None:
    """Finding 10: the ack says what was sent; the engine never re-resolves from store state."""
    store = StateStore()
    router = CommandRouter(store)
    engine = SyncEngine(store, router, tracks_for=None, log_dir=Path("/tmp/x"))  # type: ignore[arg-type]
    session = _session(engine)
    session.status = "locked"
    engine.session = session
    ack = Ack(
        correlation_id="-",
        ok=True,
        action="toggle",
        target="heos:h",
        state_version=1,
        applied=["heos:h"],
        resolved={"heos:h": "pause"},
    )
    engine._on_ack(ack)
    assert engine._mirror_queue.get_nowait() == (session, "pause", False)
    engine._on_ack(
        Ack(
            correlation_id="-",
            ok=True,
            action="toggle",
            target="heos:h",
            state_version=1,
            applied=["heos:h"],
        )
    )
    assert engine._mirror_queue.empty()  # unresolved toggle from an old router is ignored


async def test_follower_paused_from_the_app_loses_with_a_distinct_reason(rig: Rig) -> None:
    c, rt, _spy = rig
    await start_sync(c, rt)
    await c.post("/api/transport/pause", json={"target": KITCHEN})
    await wait_for(lambda: rt.store.state.sync.status == "lost", timeout=1.0)
    assert rt.store.state.sync.reason == f"{sonos_name(rt)} was paused from the app"


async def test_mirror_failure_loses(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    c, rt, _spy = rig
    await start_sync(c, rt)

    async def broken(self_, pid):
        raise ConnectionError("no sonos")

    monkeypatch.setattr(FakeSonos, "pause", broken)
    await c.post("/api/transport/pause", json={"target": HEOS_PLAYER})
    await wait_for(lambda: rt.store.state.sync.status == "lost", timeout=1.0)
    assert f"{sonos_name(rt)} didn't pause" in (rt.store.state.sync.reason or "")


async def test_new_play_on_a_synced_side_ends_the_session(rig: Rig) -> None:
    c, rt, _spy = rig
    await start_sync(c, rt)
    r = await c.post("/api/play", json={"target": KITCHEN, "content_ref": {**ALBUM, "id": "102"}})
    assert r.status_code == 200
    await wait_for(lambda: rt.store.state.sync.status == "stopped", timeout=1.0)
    assert rt.store.state.sync.reason == "playback changed"


# ---------------------------------------------------------------------------------------
# config, latency, reports, fakes
# ---------------------------------------------------------------------------------------


async def test_config_is_hot_reloadable_and_validated_before_applied(rig: Rig) -> None:
    c, rt, _spy = rig
    cfg = (await c.get("/api/sync/config")).json()
    assert cfg["drift_ms"] == 100 and cfg["monitor_s"] == 0.02
    r = await c.post("/api/sync/config", json={"drift_ms": 250, "lookahead_ms": 120})
    assert r.status_code == 200 and r.json()["drift_ms"] == 250
    assert rt.sync.config.lookahead_ms == 120 and rt.sync.config.samples == 2
    assert (await c.post("/api/sync/config", json={"drift_ms": 5})).status_code == 422
    with pytest.raises(ValueError):
        await rt.sync.update_config(samples=999)
    assert rt.sync.config.samples == 2  # nit 17: nothing changed on a bad update


async def test_start_ordering_uses_transport_latency_only() -> None:
    """Findings 4: prime and seek durations never enter the RTT estimate; the slower *transport*
    vendor fires first."""
    order: list[tuple[str, float]] = []
    loop = asyncio.get_running_loop()

    class Stub:
        def __init__(self, vendor: str) -> None:
            self.vendor = vendor

        async def start_primed(self, player_id: str, start_index: int = 0) -> None:
            order.append((self.vendor, loop.time()))

    store = StateStore()
    router = CommandRouter(store)
    lat = router.latency = LatencyTracker()
    for _ in range(3):
        lat.record("heos", 500.0, "prime")  # a slow HEOS queue load
        lat.record("sonos", 50.0, "prime")
        lat.record("heos", 40.0)  # transport
        lat.record("sonos", 45.0)
    assert lat.average("heos") == 40.0 and lat.count("heos") == 3
    assert lat.average("heos", kinds=("prime",)) == 500.0
    engine = SyncEngine(store, router, tracks_for=None, log_dir=Path("/tmp/unused"))  # type: ignore[arg-type]
    session = _session(engine)
    await engine._fire_both(session, Stub("heos"), Stub("sonos"))  # type: ignore[arg-type]
    gap_ms = (order[1][1] - order[0][1]) * 1000
    assert gap_ms < 60  # ~0: transport RTTs are within 5 ms
    order.clear()
    for _ in range(3):
        lat.record("sonos", 250.0)
    await engine._fire_both(session, Stub("heos"), Stub("sonos"))  # type: ignore[arg-type]
    assert [v for v, _ in order] == ["sonos", "heos"]
    assert 80 <= (order[1][1] - order[0][1]) * 1000 <= 400  # ~107 ms transport gap, sleep jitter


def test_report_math_and_trim(tmp_path: Path) -> None:
    t0 = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    rep = report_from_samples(
        session_id="s",
        started_at=t0,
        ended_at=t0 + timedelta(seconds=90),
        samples=[10, 20, 30, 40, 1000],
        start_delta_ms=140,
        corrections=2,
        reprimes=1,
        lost_reason=None,
        now=t0 + timedelta(seconds=200),
    )
    assert rep.duration_s == 90.0 and rep.samples == 5
    assert rep.drift_mean_ms == 220.0 and rep.drift_max_ms == 1000 and rep.drift_p95_ms == 1000.0
    empty = report_from_samples(
        session_id="e",
        started_at=t0,
        ended_at=None,
        samples=[],
        start_delta_ms=None,
        corrections=0,
        reprimes=0,
        lost_reason="x",
        now=t0 + timedelta(seconds=5),
    )
    assert empty.duration_s == 5.0 and empty.drift_mean_ms is None and empty.lost_reason == "x"
    d = tmp_path / "sync"
    d.mkdir()
    for i in range(4):
        (d / f"s{i}.json").write_text(
            f'{{"summary": {{"started_at": "2026-09-0{i + 1}T00:00:00Z"}}}}'
        )
        (d / f"s{i}.csv").write_text("t\n")
    (d / "junk.json").write_text("not json")
    removed = trim_sessions(d, keep=2)
    assert sorted(removed) == ["junk", "s0", "s1"]
    assert sorted(p.name for p in d.iterdir()) == ["s2.csv", "s2.json", "s3.csv", "s3.json"]


async def test_drift_log_flushes_and_writes_meta(tmp_path: Path) -> None:
    log = DriftLog(tmp_path / "sync", "abc")
    t = datetime.now(UTC)
    for i in range(25):
        log.add(t, i, i + 5, 5)
    assert log.needs_flush()
    await asyncio.gather(log.flush(), log.flush())  # nit 20: concurrent flushes serialise
    rows = list(csv.reader((tmp_path / "sync" / "abc.csv").open()))
    assert len(rows) == 26 and rows[1][3] == "5" and log.samples == [5] * 25


async def test_fake_heos_still_refuses_seek(store: StateStore) -> None:
    """The invariant the whole design rests on: the fake must behave like the real CLI."""
    from illyhub_hub.adapters.fake import FakeBundle

    bundle = FakeBundle(store, tick_interval_s=1.0)
    await bundle.heos.connect()
    with pytest.raises(UnsupportedCommandError):
        await bundle.heos.seek(HEOS_PLAYER, 1000)
    await bundle.heos.disconnect()


async def test_fake_ticker_rate_latency_and_confidence(store: StateStore) -> None:
    from illyhub_hub.adapters.fake import FakeBundle

    bundle = FakeBundle(store, tick_interval_s=0.01)
    await bundle.sonos.connect()
    side = bundle.sonos.side_id()
    bundle.sonos.change_track(KITCHEN)  # a track change resets the counter: next report is fresh
    bundle.sonos.sim_play_state(KITCHEN, "play")
    bundle.ticker.set_rate(side, 2.0)
    bundle.ticker.tick()  # first report after the track change: an estimate
    assert store.state.positions[side].confidence == 0.5
    for _ in range(9):
        bundle.ticker.tick()
    assert store.state.positions[side].confidence == 1.0
    assert bundle.ticker.position(side) == 200  # 10 ticks x 10 ms x 2.0
    bundle.ticker.set_seek_latency(side, 0.03)
    bundle.ticker.seek(side, 5000)
    assert bundle.ticker.position(side) == 200  # not yet landed
    await asyncio.sleep(0.05)
    assert bundle.ticker.position(side) == 5000
    assert store.state.positions[side].confidence == 0.5  # optimistic, like the real adapter
    bundle.ticker.tick()
    assert store.state.positions[side].confidence == 0.5
    bundle.ticker.tick()
    assert store.state.positions[side].confidence == 1.0
    await bundle.ticker.stop()
    await bundle.sonos.disconnect()


def test_sync_config_bounds() -> None:
    with pytest.raises(ValueError):
        SyncConfig(drift_ms=10)
    assert SyncConfig().model_dump() == {
        "drift_ms": 300,
        "samples": 2,
        "correction_gap_s": 10.0,
        "lookahead_ms": 400,
        "monitor_s": 0.5,
        "track_grace_s": 3.0,
    }
