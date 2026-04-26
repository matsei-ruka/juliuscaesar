"""`auth_pending` table CRUD on `state/gateway/queue.db`.

Tracks one outstanding session_expired re-auth round-trip per operator chat.
The unique partial index `auth_pending_one_active` enforces "at most one
waiting/redeeming row per operator chat" so back-to-back failures don't fan
out into competing prompts.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .. import queue


_AUTH_PENDING_TTL_SECONDS = 10 * 60


@dataclass(frozen=True)
class AuthPending:
    id: int
    event_id: int
    operator_chat: str
    login_url: str
    requested_at: str
    expires_at: str
    state: str
    pending_events: list[int] = field(default_factory=list)


def init_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS auth_pending (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id      INTEGER NOT NULL,
            operator_chat TEXT NOT NULL,
            login_url     TEXT NOT NULL,
            requested_at  TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            state         TEXT NOT NULL,
            pending_events TEXT NOT NULL DEFAULT '[]'
        );

        CREATE UNIQUE INDEX IF NOT EXISTS auth_pending_one_active
            ON auth_pending(operator_chat) WHERE state IN ('waiting', 'redeeming');

        CREATE INDEX IF NOT EXISTS idx_auth_pending_state
            ON auth_pending(state);
        """
    )
    conn.commit()


def _row_to_pending(row: sqlite3.Row | None) -> AuthPending | None:
    if row is None:
        return None
    raw = row["pending_events"] if "pending_events" in row.keys() else "[]"
    try:
        pending = json.loads(raw or "[]")
    except json.JSONDecodeError:
        pending = []
    if not isinstance(pending, list):
        pending = []
    return AuthPending(
        id=int(row["id"]),
        event_id=int(row["event_id"]),
        operator_chat=str(row["operator_chat"]),
        login_url=str(row["login_url"]),
        requested_at=str(row["requested_at"]),
        expires_at=str(row["expires_at"]),
        state=str(row["state"]),
        pending_events=[int(x) for x in pending if isinstance(x, (int, float))],
    )


def get_active_pending(conn: sqlite3.Connection, *, operator_chat: str) -> AuthPending | None:
    init_table(conn)
    row = conn.execute(
        """
        SELECT * FROM auth_pending
        WHERE operator_chat=? AND state IN ('waiting', 'redeeming')
        ORDER BY id DESC
        LIMIT 1
        """,
        (operator_chat,),
    ).fetchone()
    return _row_to_pending(row)


def get_by_id(conn: sqlite3.Connection, *, pending_id: int) -> AuthPending | None:
    init_table(conn)
    row = conn.execute("SELECT * FROM auth_pending WHERE id=?", (pending_id,)).fetchone()
    return _row_to_pending(row)


def insert_pending(
    conn: sqlite3.Connection,
    *,
    event_id: int,
    operator_chat: str,
    login_url: str,
    ttl_seconds: int = _AUTH_PENDING_TTL_SECONDS,
) -> AuthPending | None:
    """Insert a new waiting row. Returns None if a row already exists.

    Caller arranges to enqueue the failing event_id onto the existing row's
    pending_events when this returns None (handled by `append_pending_event`).
    """
    init_table(conn)
    requested_at = queue.now_iso()
    expires_at = queue.add_seconds(requested_at, ttl_seconds)
    try:
        cur = conn.execute(
            """
            INSERT INTO auth_pending(
                event_id, operator_chat, login_url, requested_at, expires_at, state
            ) VALUES (?, ?, ?, ?, ?, 'waiting')
            """,
            (event_id, operator_chat, login_url, requested_at, expires_at),
        )
    except sqlite3.IntegrityError:
        return None
    conn.commit()
    return get_by_id(conn, pending_id=int(cur.lastrowid))


def append_pending_event(
    conn: sqlite3.Connection,
    *,
    pending_id: int,
    event_id: int,
) -> AuthPending | None:
    """Append `event_id` to the row's pending_events list. Idempotent."""
    init_table(conn)
    pending = get_by_id(conn, pending_id=pending_id)
    if pending is None:
        return None
    if event_id == pending.event_id or event_id in pending.pending_events:
        return pending
    new_list = pending.pending_events + [event_id]
    conn.execute(
        "UPDATE auth_pending SET pending_events=? WHERE id=?",
        (json.dumps(new_list), pending_id),
    )
    conn.commit()
    return get_by_id(conn, pending_id=pending_id)


def transition(
    conn: sqlite3.Connection,
    *,
    pending_id: int,
    new_state: str,
) -> AuthPending | None:
    """Move a pending row into a new state. Returns the updated row or None."""
    init_table(conn)
    if new_state not in ("waiting", "redeeming", "done", "expired", "failed"):
        raise ValueError(f"invalid auth_pending state: {new_state!r}")
    cur = conn.execute(
        "UPDATE auth_pending SET state=? WHERE id=?",
        (new_state, pending_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_by_id(conn, pending_id=pending_id)


def expire_old(conn: sqlite3.Connection, *, now: str | None = None) -> list[AuthPending]:
    """Move every waiting/redeeming row past expires_at into state=expired."""
    init_table(conn)
    now = now or queue.now_iso()
    rows = conn.execute(
        """
        SELECT * FROM auth_pending
        WHERE state IN ('waiting', 'redeeming') AND expires_at <= ?
        """,
        (now,),
    ).fetchall()
    expired: list[AuthPending] = []
    for row in rows:
        pending = _row_to_pending(row)
        if pending is None:
            continue
        conn.execute("UPDATE auth_pending SET state='expired' WHERE id=?", (pending.id,))
        expired.append(pending)
    conn.commit()
    return expired


def fingerprint_token(token: str) -> str:
    """Return a non-reversible token identifier for audit logging.

    Format: first 4 chars + `…` + first 8 hex chars of sha256. Never log the
    full token; this is the only safe identifier callers should emit.
    """
    if not token:
        return "(empty)"
    digest = hashlib.sha256(token.encode("utf-8", errors="replace")).hexdigest()
    return f"{token[:4]}…{digest[:8]}"


_TOKEN_RE = __import__("re").compile(r"^[A-Za-z0-9._\-]{20,}$")


def looks_like_token(text: str) -> bool:
    if text is None:
        return False
    body = text.strip()
    if not body or "\n" in body or " " in body or "\t" in body:
        return False
    return bool(_TOKEN_RE.match(body))
