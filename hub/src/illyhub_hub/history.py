"""Play history: the hub's own listening log (PRD §3.4a HOME-2).

Neither HEOS nor Sonos exposes listening history, so the hub records every play it routes.
Storage is SQLite via the stdlib. :meth:`PlayHistory.open` migrates once at startup (per-version
DDL, columns verified before the version is stamped); reads and writes then run in a worker
thread with short-lived connections. ``recent()`` returns the newest play per content ref with
the targets it last went to, which is exactly what the Recently Played rail and the
target-picker pre-highlight need. Retention keeps the newest ``max_rows`` *distinct content
keys* (all their rows), so ``play_count`` never resets for something still on the rail.

Persistent write failures are surfaced through :attr:`ok` / :attr:`last_error` (→ ``/api/health``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .content import Availability, ContentRef
from .logsetup import get_logger
from .state import ArtRef

log = get_logger("history")

SCHEMA_VERSION = 3

# Every column the current schema requires; migrations must end with all of these present.
REQUIRED_COLUMNS: dict[str, str] = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "content_key": "TEXT NOT NULL DEFAULT ''",
    "content_ref_json": "TEXT NOT NULL DEFAULT '{}'",
    "service": "TEXT NOT NULL DEFAULT ''",
    "kind": "TEXT NOT NULL DEFAULT ''",
    "content_id": "TEXT NOT NULL DEFAULT ''",
    "title": "TEXT NOT NULL DEFAULT ''",
    "subtitle": "TEXT",
    "art_json": "TEXT",
    "targets_json": "TEXT NOT NULL DEFAULT '[]'",
    "side_ids_json": "TEXT NOT NULL DEFAULT '[]'",
    "played_at": "TEXT NOT NULL DEFAULT ''",
    "sync": "INTEGER NOT NULL DEFAULT 0",
    "availability_json": "TEXT",
}


class HistoryItem(BaseModel):
    content_ref: ContentRef
    title: str
    subtitle: str | None = None
    art: ArtRef = Field(default_factory=ArtRef)
    last_played_at: datetime
    last_targets: list[str] = Field(default_factory=list, description="side ids of the last play")
    play_count: int = 1
    sync: bool = False
    # Which ecosystems could play this at the time of the play; the API refreshes it from the
    # live browse caches when serving, so the recents rail can grey a vendor that lost the item.
    availability: Availability | None = None


class MigrationError(RuntimeError):
    pass


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(plays)")}


def _current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is not None:
        return int(row[0])
    # No stamp: v0 is "a legacy plays table exists", -1 is "nothing at all".
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plays'"
    ).fetchone()
    return 0 if has_table else -1


def _migrate_to_1(conn: sqlite3.Connection) -> None:
    """v1: create the table (fresh install) or ALTER a legacy v0 table up to the v1 columns."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_key TEXT NOT NULL DEFAULT '',
            content_ref_json TEXT NOT NULL DEFAULT '{}',
            service TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '',
            content_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            subtitle TEXT,
            art_json TEXT,
            targets_json TEXT NOT NULL DEFAULT '[]',
            side_ids_json TEXT NOT NULL DEFAULT '[]',
            played_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    have = _columns(conn)
    for name, ddl in REQUIRED_COLUMNS.items():
        if name in have or name in {"id", "sync"}:
            continue
        conn.execute(f"ALTER TABLE plays ADD COLUMN {name} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS plays_played_at ON plays (played_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS plays_content_key ON plays (content_key)")


def _migrate_to_2(conn: sqlite3.Connection) -> None:
    """v2: the ``sync`` flag (Sync Play rows)."""
    if "sync" not in _columns(conn):
        conn.execute("ALTER TABLE plays ADD COLUMN sync INTEGER NOT NULL DEFAULT 0")


def _migrate_to_3(conn: sqlite3.Connection) -> None:
    """v3: per-vendor ``availability`` snapshot (Phase 5)."""
    if "availability_json" not in _columns(conn):
        conn.execute("ALTER TABLE plays ADD COLUMN availability_json TEXT")


MIGRATIONS = {1: _migrate_to_1, 2: _migrate_to_2, 3: _migrate_to_3}


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the schema to :data:`SCHEMA_VERSION`. Returns the version migrated *from*.

    Each step runs its DDL, then the resulting columns are verified before the version is
    stamped, so a half-applied migration is an error rather than a silent lie.
    """
    start = _current_version(conn)
    version = max(start, 0)
    while version < SCHEMA_VERSION:
        version += 1
        MIGRATIONS[version](conn)
    missing = set(REQUIRED_COLUMNS) - _columns(conn)
    if missing:
        conn.rollback()
        raise MigrationError(
            f"history schema is missing columns after migration: {sorted(missing)}"
        )
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()
    if start != SCHEMA_VERSION:
        log.info("history schema migrated", extra={"extra": {"from": start, "to": SCHEMA_VERSION}})
    return start


class PlayHistory:
    def __init__(self, path: Path, *, max_rows: int = 500) -> None:
        self.path = path
        self.max_rows = max_rows
        self.last_error: str | None = None
        self._opened = False

    @property
    def ok(self) -> bool:
        return self._opened and self.last_error is None

    # -- sync core (runs in a worker thread) ------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path)

    def _open(self) -> None:
        with contextlib.closing(self._connect()) as conn:
            migrate(conn)

    def _record(
        self,
        ref: ContentRef,
        title: str,
        subtitle: str | None,
        art: ArtRef | None,
        targets: Sequence[str],
        side_ids: Sequence[str],
        sync: bool,
        played_at: datetime,
        availability: Availability | None = None,
    ) -> None:
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO plays (content_key, content_ref_json, service, kind, content_id, title,
                                   subtitle, art_json, targets_json, side_ids_json, played_at, sync,
                                   availability_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref.key,
                    ref.model_dump_json(),
                    ref.service,
                    ref.kind,
                    ref.id,
                    title,
                    subtitle,
                    art.model_dump_json() if art else None,
                    json.dumps(list(targets)),
                    json.dumps(list(side_ids)),
                    played_at.isoformat(),
                    1 if sync else 0,
                    availability.model_dump_json() if availability else None,
                ),
            )
            # Retention by distinct content: keep every row of the newest N content keys.
            conn.execute(
                """
                DELETE FROM plays WHERE content_key NOT IN (
                    SELECT content_key FROM (
                        SELECT content_key, MAX(id) AS last_id FROM plays
                        GROUP BY content_key ORDER BY last_id DESC LIMIT ?
                    )
                )
                """,
                (self.max_rows,),
            )

    def _recent(self, limit: int) -> list[HistoryItem]:
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT p.content_ref_json, p.title, p.subtitle, p.art_json, p.side_ids_json,
                       p.played_at, p.sync, c.n, p.availability_json
                FROM plays p
                JOIN (
                    SELECT content_key, MAX(id) AS last_id, COUNT(*) AS n
                    FROM plays GROUP BY content_key
                ) c ON c.last_id = p.id
                ORDER BY p.played_at DESC, p.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        items: list[HistoryItem] = []
        for ref_json, title, subtitle, art_json, sides_json, played_at, sync, n, avail in rows:
            items.append(
                HistoryItem(
                    content_ref=ContentRef.model_validate_json(ref_json),
                    title=title,
                    subtitle=subtitle,
                    art=ArtRef.model_validate_json(art_json) if art_json else ArtRef(),
                    last_played_at=datetime.fromisoformat(played_at),
                    last_targets=json.loads(sides_json),
                    play_count=int(n),
                    sync=bool(sync),
                    availability=Availability.model_validate_json(avail) if avail else None,
                )
            )
        return items

    def _count(self) -> int:
        with contextlib.closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0])

    def _clear(self) -> None:
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM plays")

    # -- async surface ------------------------------------------------------------------

    async def open(self) -> bool:
        """Migrate once. Returns False (and records ``last_error``) when the file is unusable."""
        try:
            await asyncio.to_thread(self._open)
        except (sqlite3.Error, MigrationError, OSError) as exc:
            self.last_error = f"history unavailable: {exc}"
            log.exception("history open failed", extra={"extra": {"path": str(self.path)}})
            self._opened = False
            return False
        self._opened = True
        self.last_error = None
        return True

    async def record(
        self,
        ref: ContentRef,
        *,
        title: str,
        subtitle: str | None = None,
        art: ArtRef | None = None,
        targets: Sequence[str] = (),
        side_ids: Sequence[str] = (),
        sync: bool = False,
        played_at: datetime | None = None,
        availability: Availability | None = None,
    ) -> bool:
        if not self._opened and not await self.open():
            return False
        when = played_at or datetime.now(UTC)
        try:
            await asyncio.to_thread(
                self._record, ref, title, subtitle, art, targets, side_ids, sync, when, availability
            )
        except sqlite3.Error as exc:
            self.last_error = f"history write failed: {exc}"
            log.exception("history write failed", extra={"extra": {"content": ref.key}})
            return False
        self.last_error = None
        return True

    async def recent(self, limit: int = 20) -> list[HistoryItem]:
        if not self._opened and not await self.open():
            return []
        try:
            return await asyncio.to_thread(self._recent, max(1, min(limit, 200)))
        except sqlite3.Error as exc:
            self.last_error = f"history read failed: {exc}"
            log.exception("history read failed")
            return []

    async def count(self) -> int:
        if not self._opened and not await self.open():
            return 0
        try:
            return await asyncio.to_thread(self._count)
        except sqlite3.Error:
            log.exception("history count failed")
            return 0

    async def clear(self) -> None:
        if not self._opened and not await self.open():
            return
        try:
            await asyncio.to_thread(self._clear)
        except sqlite3.Error:
            log.exception("history clear failed")
