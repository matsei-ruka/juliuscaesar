"""Durable SQLite event queue for the JuliusCaesar gateway.

The queue is intentionally local-first: one SQLite database under
<instance>/state/gateway/queue.db. Transactions stay short; brain or channel
work must happen after a claim transaction commits.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 4
DEFAULT_RETRY_BACKOFF_SECONDS = (10, 60, 300)


@dataclass(frozen=True)
class Event:
    id: int
    source: str
    source_message_id: str | None
    user_id: str | None
    conversation_id: str | None
    content: str
    meta: str | None
    status: str
    received_at: str
    available_at: str
    locked_by: str | None
    locked_until: str | None
    started_at: str | None
    finished_at: str | None
    retry_count: int
    response: str | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.meta:
            try:
                data["meta"] = json.loads(self.meta)
            except json.JSONDecodeError:
                pass
        return data


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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def add_seconds(ts: str, seconds: int) -> str:
    if ts.endswith("Z"):
        base = datetime.fromisoformat(ts[:-1] + "+00:00")
    else:
        base = datetime.fromisoformat(ts)
    return (base + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


def queue_dir(instance_dir: Path) -> Path:
    return instance_dir / "state" / "gateway"


def queue_path(instance_dir: Path) -> Path:
    return queue_dir(instance_dir) / "queue.db"


def connect(instance_dir: Path) -> sqlite3.Connection:
    queue_dir(instance_dir).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(queue_path(instance_dir), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        INSERT OR IGNORE INTO meta(key, value)
        VALUES ('schema_version', '4');

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_message_id TEXT,
            user_id TEXT,
            conversation_id TEXT,
            content TEXT NOT NULL,
            meta TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            received_at TEXT NOT NULL,
            available_at TEXT NOT NULL,
            locked_by TEXT,
            locked_until TEXT,
            started_at TEXT,
            finished_at TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            response TEXT,
            error TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup
        ON events(source, source_message_id)
        WHERE source_message_id IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_events_ready
        ON events(status, available_at, id);

        CREATE INDEX IF NOT EXISTS idx_events_lock
        ON events(locked_until);

        CREATE INDEX IF NOT EXISTS idx_events_conversation
        ON events(source, user_id, conversation_id, received_at DESC);

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

        CREATE TABLE IF NOT EXISTS chats (
            channel          TEXT NOT NULL,
            chat_id          TEXT NOT NULL,
            chat_type        TEXT,
            title            TEXT,
            username         TEXT,
            member_count     INTEGER,
            first_seen       TEXT NOT NULL,
            last_seen        TEXT NOT NULL,
            last_message_id  TEXT,
            auth_status      TEXT NOT NULL DEFAULT 'allowed',
            PRIMARY KEY (channel, chat_id)
        );

        CREATE INDEX IF NOT EXISTS idx_chats_last_seen
        ON chats(channel, last_seen DESC);
        """
    )
    add_column_if_missing(
        conn,
        table="chats",
        column="auth_status",
        column_ddl="TEXT NOT NULL DEFAULT 'allowed'",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chats_auth_status "
        "ON chats(channel, auth_status)"
    )
    conn.execute(
        "UPDATE meta SET value=? WHERE key='schema_version'",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def add_column_if_missing(
    conn: sqlite3.Connection,
    *,
    table: str,
    column: str,
    column_ddl: str,
) -> bool:
    """Idempotent `ALTER TABLE ADD COLUMN` for SQLite (no native IF NOT EXISTS).

    Returns True iff the column was just added.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column in cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_ddl}")
    return True


def row_to_event(row: sqlite3.Row | None) -> Event | None:
    if row is None:
        return None
    return Event(**{key: row[key] for key in row.keys()})


def row_to_session(row: sqlite3.Row | None) -> Session | None:
    if row is None:
        return None
    return Session(**{key: row[key] for key in row.keys()})


def encode_meta(meta: dict[str, Any] | None) -> str | None:
    if meta is None:
        return None
    return json.dumps(meta, sort_keys=True, separators=(",", ":"))


def enqueue(
    conn: sqlite3.Connection,
    *,
    source: str,
    content: str,
    source_message_id: str | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    meta: dict[str, Any] | None = None,
    available_at: str | None = None,
) -> tuple[Event, bool]:
    """Insert an event.

    Returns (event, inserted). If a source/source_message_id duplicate already
    exists, returns the existing event with inserted=False.
    """

    ts = now_iso()
    available_at = available_at or ts
    meta_text = encode_meta(meta)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO events(
            source, source_message_id, user_id, conversation_id, content, meta,
            status, received_at, available_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
        """,
        (source, source_message_id, user_id, conversation_id, content, meta_text, ts, available_at),
    )
    conn.commit()

    inserted = cur.rowcount == 1
    if inserted:
        row = conn.execute("SELECT * FROM events WHERE id=?", (cur.lastrowid,)).fetchone()
    elif source_message_id is not None:
        row = conn.execute(
            "SELECT * FROM events WHERE source=? AND source_message_id=?",
            (source, source_message_id),
        ).fetchone()
    else:
        row = None
    event = row_to_event(row)
    if event is None:
        raise RuntimeError("failed to read enqueued event")
    return event, inserted


def requeue_expired(conn: sqlite3.Connection, *, now: str | None = None) -> list[int]:
    """Move every `running` event whose lease has expired back to `queued`.

    Returns the ids of the requeued events (in id order). Caller can `len()`
    for a count, or iterate for log/audit. Snapshot the candidates first so
    the diagnostic log can name the rows that were actually moved — knowing
    `which` events expired is the key signal for debugging dispatch hangs.
    """

    now = now or now_iso()
    rows = conn.execute(
        """
        SELECT id FROM events
        WHERE status='running'
          AND locked_until IS NOT NULL
          AND locked_until <= ?
        ORDER BY id
        """,
        (now,),
    ).fetchall()
    ids = [int(row["id"]) for row in rows]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"""
        UPDATE events
        SET status='queued',
            available_at=?,
            locked_by=NULL,
            locked_until=NULL,
            error=COALESCE(error, 'lease expired')
        WHERE id IN ({placeholders})
        """,
        [now, *ids],
    )
    return ids


def claim_next(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    lease_seconds: int = 300,
    sources: Iterable[str] | None = None,
) -> Event | None:
    """Claim the next ready event with a short SQLite write transaction."""

    now = now_iso()
    locked_until = add_seconds(now, lease_seconds)
    source_list = list(sources or [])
    params: list[Any] = [now]
    source_clause = ""
    if source_list:
        source_clause = f" AND source IN ({','.join('?' for _ in source_list)})"
        params.extend(source_list)

    try:
        conn.execute("BEGIN IMMEDIATE")
        requeue_expired(conn, now=now)
        row = conn.execute(
            f"""
            SELECT id FROM events
            WHERE status='queued'
              AND available_at <= ?
              {source_clause}
            ORDER BY id
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            conn.commit()
            return None

        event_id = int(row["id"])
        conn.execute(
            """
            UPDATE events
            SET status='running',
                locked_by=?,
                locked_until=?,
                started_at=COALESCE(started_at, ?),
                error=NULL
            WHERE id=?
            """,
            (worker_id, locked_until, now, event_id),
        )
        event = row_to_event(conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())
        conn.commit()
        return event
    except Exception:
        conn.rollback()
        raise


def complete(conn: sqlite3.Connection, event_id: int, *, response: str = "") -> Event:
    ts = now_iso()
    conn.execute(
        """
        UPDATE events
        SET status='done',
            finished_at=?,
            locked_by=NULL,
            locked_until=NULL,
            response=?,
            error=NULL
        WHERE id=?
        """,
        (ts, response, event_id),
    )
    conn.commit()
    event = row_to_event(conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())
    if event is None:
        raise KeyError(event_id)
    return event


def fail(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    error: str,
    max_retries: int = 3,
    backoff_seconds: tuple[int, ...] = DEFAULT_RETRY_BACKOFF_SECONDS,
) -> Event:
    row = conn.execute("SELECT retry_count FROM events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise KeyError(event_id)

    retry_count = int(row["retry_count"]) + 1
    ts = now_iso()
    if retry_count <= max_retries:
        delay = backoff_seconds[min(retry_count - 1, len(backoff_seconds) - 1)]
        conn.execute(
            """
            UPDATE events
            SET status='queued',
                retry_count=?,
                available_at=?,
                locked_by=NULL,
                locked_until=NULL,
                error=?
            WHERE id=?
            """,
            (retry_count, add_seconds(ts, delay), error, event_id),
        )
    else:
        conn.execute(
            """
            UPDATE events
            SET status='failed',
                retry_count=?,
                finished_at=?,
                locked_by=NULL,
                locked_until=NULL,
                error=?
            WHERE id=?
            """,
            (retry_count, ts, error, event_id),
        )
    conn.commit()
    event = row_to_event(conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())
    if event is None:
        raise KeyError(event_id)
    return event


def retry_now(conn: sqlite3.Connection, event_id: int) -> Event:
    ts = now_iso()
    cur = conn.execute(
        """
        UPDATE events
        SET status='queued',
            available_at=?,
            locked_by=NULL,
            locked_until=NULL,
            finished_at=NULL,
            error=NULL
        WHERE id=?
        """,
        (ts, event_id),
    )
    if cur.rowcount != 1:
        raise KeyError(event_id)
    conn.commit()
    event = get(conn, event_id)
    if event is None:
        raise KeyError(event_id)
    return event


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM events GROUP BY status").fetchall()
    return {str(row["status"]): int(row["n"]) for row in rows}


def recent(conn: sqlite3.Connection, *, limit: int = 20) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [event for row in rows if (event := row_to_event(row)) is not None]


def get(conn: sqlite3.Connection, event_id: int) -> Event | None:
    return row_to_event(conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())


def get_session(
    conn: sqlite3.Connection,
    *,
    channel: str,
    conversation_id: str,
    brain: str,
) -> Session | None:
    return row_to_session(
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
