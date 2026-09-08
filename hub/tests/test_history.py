from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from illyhub_hub.content import ContentRef
from illyhub_hub.history import REQUIRED_COLUMNS, SCHEMA_VERSION, PlayHistory, migrate
from illyhub_hub.state import ArtRef

ALBUM = ContentRef(service="tidal", kind="album", id="101")
PLAYLIST = ContentRef(service="tidal", kind="playlist", id="p-1")


async def test_record_and_recent_dedupes_by_content_and_keeps_last_targets(tmp_path: Path) -> None:
    h = PlayHistory(tmp_path / "h.sqlite", max_rows=500)
    t0 = datetime(2026, 9, 7, 20, 0, tzinfo=UTC)
    await h.record(
        ALBUM, title="Warm Glow", subtitle="Analog Heart", side_ids=["heos:heos-1"], played_at=t0
    )
    await h.record(
        PLAYLIST, title="Dinner party", side_ids=["sonos:k"], played_at=t0 + timedelta(minutes=1)
    )
    await h.record(
        ALBUM,
        title="Warm Glow",
        art=ArtRef(url="/api/art/abc", cache_key="abc"),
        side_ids=["sonos:k", "heos:heos-1"],
        sync=True,
        played_at=t0 + timedelta(minutes=2),
    )
    items = await h.recent(10)
    assert [i.content_ref.key for i in items] == ["tidal:album:101", "tidal:playlist:p-1"]
    album = items[0]
    assert album.play_count == 2 and album.last_targets == ["sonos:k", "heos:heos-1"]
    assert album.sync is True and album.art.cache_key == "abc"
    assert album.last_played_at == t0 + timedelta(minutes=2)
    assert items[1].play_count == 1 and items[1].subtitle is None
    assert await h.count() == 3


async def test_retention_keeps_newest_distinct_content_and_play_counts(tmp_path: Path) -> None:
    h = PlayHistory(tmp_path / "h.sqlite", max_rows=3)
    t0 = datetime(2026, 9, 7, tzinfo=UTC)
    fav = ContentRef(service="tidal", kind="album", id="fav")
    for i in range(4):  # the favourite is played four times first
        await h.record(fav, title="Fav", played_at=t0 + timedelta(minutes=i))
    for i in range(5):
        ref = ContentRef(service="tidal", kind="album", id=str(i))
        await h.record(ref, title=f"A{i}", played_at=t0 + timedelta(minutes=10 + i))
    # 3 distinct keys survive: the two newest albums plus... the favourite was pushed out.
    items = await h.recent(10)
    assert [i.content_ref.id for i in items] == ["4", "3", "2"]
    # Re-play the favourite: it returns with its history intact? No — its rows were evicted as
    # a whole; what must never happen is a *surviving* key losing rows.
    await h.record(ref, title="A4", played_at=t0 + timedelta(minutes=30))
    items = await h.recent(10)
    assert items[0].content_ref.id == "4" and items[0].play_count == 2
    assert await h.count() == 4  # 2 rows for "4", 1 each for "3" and "2"


def test_migrate_from_a_legacy_v0_table(tmp_path: Path) -> None:
    path = tmp_path / "v0.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE plays (id INTEGER PRIMARY KEY AUTOINCREMENT, content_key TEXT NOT NULL,
           content_ref_json TEXT NOT NULL, service TEXT NOT NULL, kind TEXT NOT NULL,
           content_id TEXT NOT NULL, title TEXT NOT NULL, subtitle TEXT, art_json TEXT,
           targets_json TEXT NOT NULL, side_ids_json TEXT NOT NULL, played_at TEXT NOT NULL)"""
    )
    conn.execute(
        "INSERT INTO plays VALUES (1, 'tidal:album:1', '{\"service\":\"tidal\",\"kind\":\"album\",\"id\":\"1\"}', 'tidal', 'album', '1', 'Old', NULL, NULL, '[]', '[\"heos:heos-1\"]', '2026-09-01T00:00:00+00:00')"
    )
    conn.commit()
    assert migrate(conn) == 0  # migrated from v0
    cols = {r[1] for r in conn.execute("PRAGMA table_info(plays)")}
    assert "sync" in cols and set(REQUIRED_COLUMNS) <= cols
    assert conn.execute("SELECT version FROM schema_version").fetchone() == (SCHEMA_VERSION,)
    assert conn.execute("SELECT sync FROM plays").fetchone() == (0,)
    assert migrate(conn) == SCHEMA_VERSION  # idempotent, already current
    conn.close()


async def test_open_reports_unusable_file(tmp_path: Path) -> None:
    path = tmp_path / "bad.sqlite"
    path.write_text("not a database")
    h = PlayHistory(path)
    assert await h.open() is False and h.ok is False and "history unavailable" in h.last_error
    assert await h.record(ALBUM, title="x") is False


async def test_limit_is_clamped_and_clear_works(tmp_path: Path) -> None:
    h = PlayHistory(tmp_path / "h.sqlite")
    await h.record(ALBUM, title="x")
    assert len(await h.recent(0)) == 1  # clamped to 1
    await h.clear()
    assert await h.recent() == [] and await h.count() == 0


def test_migrate_is_idempotent_and_versioned(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "m.sqlite")
    migrate(conn)
    migrate(conn)
    assert conn.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
    cols = {r[1] for r in conn.execute("PRAGMA table_info(plays)")}
    assert {"content_key", "targets_json", "side_ids_json", "played_at", "sync"} <= cols


async def test_read_failure_is_logged_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "bad.sqlite"
    path.write_text("not a database")
    h = PlayHistory(path)
    assert await h.recent() == []  # sqlite3.DatabaseError swallowed
    assert await h.record(ALBUM, title="x") is False  # write failure also swallowed
    assert await h.count() == 0
    await h.clear()
