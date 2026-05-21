"""Session manager for the gateway.

Stores per-(channel, conversation_id, brain, slot) native session ids so
subsequent messages can resume the same brain conversation chain. The slot
axis exists for parallel dispatch (docs/specs/parallel-slots.md); for the
default `max_concurrent: 1` config every row sits at slot 0 and behavior is
identical to the pre-parallel-slots schema.

Also tracks a sticky-brain window per (channel, conversation_id) so the
triage layer (Sprint 4) does not re-classify mid-conversation.

The session and sticky tables share the same SQLite database as the event
queue (`<instance>/state/gateway/queue.db`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class Session:
    id: int
    channel: str
    conversation_id: str
    brain: str
    slot: int
    session_id: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StickyBrain:
    channel: str
    conversation_id: str
    brain: str
    sticky_until: str
    updated_at: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def add_seconds(ts: str, seconds: int) -> str:
    if ts.endswith("Z"):
        base = datetime.fromisoformat(ts[:-1] + "+00:00")
    else:
        base = datetime.fromisoformat(ts)
    return (base + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            brain TEXT NOT NULL,
            slot INTEGER NOT NULL DEFAULT 0,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(channel, conversation_id, brain, slot)
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_updated
        ON sessions(updated_at DESC);

        CREATE TABLE IF NOT EXISTS sticky_brain (
            channel TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            brain TEXT NOT NULL,
            sticky_until TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (channel, conversation_id)
        );

        CREATE INDEX IF NOT EXISTS idx_sticky_until
        ON sticky_brain(sticky_until);
        """
    )
    _migrate_add_slot(conn)
    conn.commit()


def _migrate_add_slot(conn: sqlite3.Connection) -> bool:
    """Idempotent migration: add `slot` column + new UNIQUE constraint.

    Existing rows pre-parallel-slots used UNIQUE(channel, conversation_id,
    brain). SQLite can't ALTER a UNIQUE constraint in place, so we rebuild
    the table when `slot` is missing. Pre-existing rows get slot=0, matching
    serial behavior.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "slot" in cols:
        return False
    conn.executescript(
        """
        CREATE TABLE sessions_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            brain TEXT NOT NULL,
            slot INTEGER NOT NULL DEFAULT 0,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(channel, conversation_id, brain, slot)
        );

        INSERT INTO sessions_new
            (id, channel, conversation_id, brain, slot, session_id, created_at, updated_at)
        SELECT
            id, channel, conversation_id, brain, 0, session_id, created_at, updated_at
        FROM sessions;

        DROP TABLE sessions;

        ALTER TABLE sessions_new RENAME TO sessions;

        CREATE INDEX IF NOT EXISTS idx_sessions_updated
        ON sessions(updated_at DESC);
        """
    )
    return True


def _row_to_session(row: sqlite3.Row | None) -> Session | None:
    if row is None:
        return None
    return Session(**{key: row[key] for key in row.keys()})


def get_session(
    conn: sqlite3.Connection,
    *,
    channel: str,
    conversation_id: str,
    brain: str,
    slot: int = 0,
) -> Session | None:
    return _row_to_session(
        conn.execute(
            """
            SELECT * FROM sessions
            WHERE channel=? AND conversation_id=? AND brain=? AND slot=?
            """,
            (channel, conversation_id, brain, int(slot)),
        ).fetchone()
    )


def list_sessions_for_conversation(
    conn: sqlite3.Connection,
    *,
    channel: str,
    conversation_id: str,
    brain: str,
) -> list[Session]:
    """All slot rows for a conversation + brain, ordered by slot ascending.

    Used by the parallel-slots dispatcher to decide which slot a new event
    should land on. Empty when no slot has captured a session yet (cold
    conversation).
    """
    rows = conn.execute(
        """
        SELECT * FROM sessions
        WHERE channel=? AND conversation_id=? AND brain=?
        ORDER BY slot ASC
        """,
        (channel, conversation_id, brain),
    ).fetchall()
    return [s for row in rows if (s := _row_to_session(row)) is not None]


def upsert_session(
    conn: sqlite3.Connection,
    *,
    channel: str,
    conversation_id: str,
    brain: str,
    session_id: str,
    slot: int = 0,
) -> Session:
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO sessions(channel, conversation_id, brain, slot, session_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel, conversation_id, brain, slot) DO UPDATE SET
            session_id=excluded.session_id,
            updated_at=excluded.updated_at
        """,
        (channel, conversation_id, brain, int(slot), session_id, ts, ts),
    )
    conn.commit()
    session = get_session(
        conn,
        channel=channel,
        conversation_id=conversation_id,
        brain=brain,
        slot=slot,
    )
    if session is None:
        raise RuntimeError("failed to read upserted session")
    return session


def get_active_sticky(
    conn: sqlite3.Connection,
    *,
    channel: str,
    conversation_id: str,
    now: str | None = None,
) -> StickyBrain | None:
    init_db(conn)
    now = now or now_iso()
    row = conn.execute(
        """
        SELECT channel, conversation_id, brain, sticky_until, updated_at
        FROM sticky_brain
        WHERE channel=? AND conversation_id=? AND sticky_until > ?
        """,
        (channel, conversation_id, now),
    ).fetchone()
    if row is None:
        return None
    return StickyBrain(**{key: row[key] for key in row.keys()})


def record_response(
    conn: sqlite3.Connection,
    *,
    channel: str,
    conversation_id: str,
    brain: str,
    sticky_idle_seconds: int,
) -> None:
    init_db(conn)
    ts = now_iso()
    sticky_until = add_seconds(ts, sticky_idle_seconds)
    conn.execute(
        """
        INSERT INTO sticky_brain(channel, conversation_id, brain, sticky_until, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(channel, conversation_id) DO UPDATE SET
            brain=excluded.brain,
            sticky_until=excluded.sticky_until,
            updated_at=excluded.updated_at
        """,
        (channel, conversation_id, brain, sticky_until, ts),
    )
    conn.commit()


def purge_idle(conn: sqlite3.Connection, *, older_than_seconds: int = 60 * 60 * 24 * 7) -> int:
    init_db(conn)
    cutoff = add_seconds(now_iso(), -older_than_seconds)
    cur = conn.execute(
        "DELETE FROM sticky_brain WHERE sticky_until < ?",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount
