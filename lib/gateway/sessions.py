"""Session manager for the gateway.

Stores per-(channel, conversation_id, brain) native session ids so subsequent
messages can resume the same brain conversation. Also tracks a sticky-brain
window per (channel, conversation_id) so the triage layer (Sprint 4) does not
re-classify mid-conversation.

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
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(channel, conversation_id, brain)
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
    conn.commit()


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
) -> Session | None:
    return _row_to_session(
        conn.execute(
            """
            SELECT * FROM sessions
            WHERE channel=? AND conversation_id=? AND brain=?
            """,
            (channel, conversation_id, brain),
        ).fetchone()
    )


def upsert_session(
    conn: sqlite3.Connection,
    *,
    channel: str,
    conversation_id: str,
    brain: str,
    session_id: str,
) -> Session:
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO sessions(channel, conversation_id, brain, session_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel, conversation_id, brain) DO UPDATE SET
            session_id=excluded.session_id,
            updated_at=excluded.updated_at
        """,
        (channel, conversation_id, brain, session_id, ts, ts),
    )
    conn.commit()
    session = get_session(conn, channel=channel, conversation_id=conversation_id, brain=brain)
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
