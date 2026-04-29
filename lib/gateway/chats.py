"""Chat directory backed by the gateway queue DB.

Tracks every chat (DM, group, supergroup, channel) the gateway has seen on a
given channel. Rows are upserted on every inbound message. Reading the table
gives the brain awareness of the full chat universe, not only the conversation
that produced the active event.

The `channel` column is intentionally generic so a future Discord/Slack
adapter can write here too. Telegram is the only writer today.

Connection model: the public read/write functions accept either an open
`conn` (preferred — long-lived gateway threads cache one) or an
`instance_dir` (one-shot — for CLI tools). Passing `conn` avoids the
init_db churn that otherwise runs on every call.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import queue


L1_CHATS_FILENAME = "CHATS.md"
L1_CHATS_HEADER = "<!-- AUTO-GENERATED — do not edit; rebuilt from gateway queue.db -->"
_REGEN_DEBOUNCE_SECONDS = 30.0
_LAST_REGEN: dict[str, float] = {}
_REGEN_LOCK = threading.Lock()

VALID_AUTH_STATUSES = ("allowed", "pending", "denied")


@dataclass(frozen=True)
class Chat:
    channel: str
    chat_id: str
    chat_type: str | None
    title: str | None
    username: str | None
    member_count: int | None
    first_seen: str
    last_seen: str
    last_message_id: str | None
    auth_status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _row_to_chat(row: sqlite3.Row | None) -> Chat | None:
    if row is None:
        return None
    keys = row.keys()
    data = {key: row[key] for key in keys}
    # Older DB rows (pre-schema-4) may not carry auth_status; default to
    # pending so unknown chats are not auto-authorized.
    data.setdefault("auth_status", "pending")
    if data.get("auth_status") is None:
        data["auth_status"] = "pending"
    return Chat(**data)


@contextmanager
def _scoped_conn(
    instance_dir: Path | None,
    conn: sqlite3.Connection | None,
) -> Iterator[sqlite3.Connection]:
    """Yield a connection — provided or freshly opened.

    Closes the connection only when this function opened it.
    """
    if conn is not None:
        yield conn
        return
    if instance_dir is None:
        raise TypeError("either instance_dir or conn must be provided")
    own = queue.connect(instance_dir)
    try:
        yield own
    finally:
        own.close()


def upsert_chat(
    instance_dir: Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    channel: str,
    chat_id: str,
    chat_type: str | None = None,
    title: str | None = None,
    username: str | None = None,
    member_count: int | None = None,
    last_message_id: str | None = None,
    auth_status: str | None = None,
) -> Chat:
    """Insert or refresh a chat row.

    On conflict, `first_seen` is preserved, `last_seen` is bumped to now,
    and the optional fields (`chat_type`, `title`, `username`,
    `member_count`, `last_message_id`, `auth_status`) are only overwritten
    when the new value is non-NULL — a transiently missing field in one
    update must not wipe a previously-known value.
    """

    if auth_status is not None and auth_status not in VALID_AUTH_STATUSES:
        raise ValueError(
            f"auth_status must be one of {VALID_AUTH_STATUSES}, got {auth_status!r}"
        )

    ts = queue.now_iso()
    with _scoped_conn(instance_dir, conn) as c:
        c.execute(
            """
            INSERT INTO chats(
                channel, chat_id, chat_type, title, username,
                member_count, first_seen, last_seen, last_message_id,
                auth_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'pending'))
            ON CONFLICT(channel, chat_id) DO UPDATE SET
                chat_type       = COALESCE(excluded.chat_type, chats.chat_type),
                title           = COALESCE(excluded.title, chats.title),
                username        = COALESCE(excluded.username, chats.username),
                member_count    = COALESCE(excluded.member_count, chats.member_count),
                last_seen       = excluded.last_seen,
                last_message_id = COALESCE(excluded.last_message_id, chats.last_message_id),
                auth_status     = COALESCE(?, chats.auth_status)
            """,
            (
                channel,
                chat_id,
                chat_type,
                title,
                username,
                member_count,
                ts,
                ts,
                last_message_id,
                auth_status,
                auth_status,
            ),
        )
        c.commit()
        chat = _row_to_chat(
            c.execute(
                "SELECT * FROM chats WHERE channel=? AND chat_id=?",
                (channel, chat_id),
            ).fetchone()
        )
    if chat is None:
        raise RuntimeError("failed to read upserted chat")
    if instance_dir is not None:
        _maybe_regenerate_l1_chats(instance_dir)
    return chat


def set_auth_status(
    instance_dir: Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    channel: str,
    chat_id: str,
    status: str,
) -> Chat | None:
    """Flip a chat's auth_status. Returns the updated row, or None if absent."""
    if status not in VALID_AUTH_STATUSES:
        raise ValueError(
            f"status must be one of {VALID_AUTH_STATUSES}, got {status!r}"
        )
    ts = queue.now_iso()
    with _scoped_conn(instance_dir, conn) as c:
        cur = c.execute(
            "UPDATE chats SET auth_status=?, last_seen=? WHERE channel=? AND chat_id=?",
            (status, ts, channel, chat_id),
        )
        c.commit()
        if cur.rowcount == 0:
            return None
        return _row_to_chat(
            c.execute(
                "SELECT * FROM chats WHERE channel=? AND chat_id=?",
                (channel, chat_id),
            ).fetchone()
        )


def get_chat(
    instance_dir: Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    channel: str,
    chat_id: str,
) -> Chat | None:
    with _scoped_conn(instance_dir, conn) as c:
        return _row_to_chat(
            c.execute(
                "SELECT * FROM chats WHERE channel=? AND chat_id=?",
                (channel, chat_id),
            ).fetchone()
        )


def list_chats(
    instance_dir: Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    channel: str | None = None,
    auth_status: str | None = None,
    limit: int | None = None,
) -> list[Chat]:
    """Return chats ordered by `last_seen DESC`."""
    with _scoped_conn(instance_dir, conn) as c:
        params: list[Any] = []
        clauses: list[str] = []
        if channel is not None:
            clauses.append("channel=?")
            params.append(channel)
        if auth_status is not None:
            if auth_status not in VALID_AUTH_STATUSES:
                raise ValueError(
                    f"auth_status must be one of {VALID_AUTH_STATUSES}, got {auth_status!r}"
                )
            clauses.append("auth_status=?")
            params.append(auth_status)
        sql = "SELECT * FROM chats"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY last_seen DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = c.execute(sql, params).fetchall()
    return [chat for row in rows if (chat := _row_to_chat(row)) is not None]


def pending_chats(
    instance_dir: Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    channel: str | None = None,
) -> list[Chat]:
    """Convenience: chats awaiting auth, oldest-first (caller should review)."""
    rows = list_chats(
        instance_dir, conn=conn, channel=channel, auth_status="pending"
    )
    rows.sort(key=lambda c: c.first_seen)
    return rows


def _l1_chats_path(instance_dir: Path) -> Path:
    return instance_dir / "memory" / "L1" / L1_CHATS_FILENAME


def _format_l1_chats(rows: list[Chat]) -> str:
    lines = [
        L1_CHATS_HEADER,
        "",
        "# Known Telegram chats",
        "",
    ]
    if not rows:
        lines.append("(no chats recorded yet)")
        return "\n".join(lines) + "\n"
    for chat in rows:
        ctype = chat.chat_type or "?"
        title = chat.title or "(untitled)"
        handle = f" (@{chat.username})" if chat.username else ""
        members = (
            f" ({chat.member_count} members)"
            if chat.member_count is not None
            else ""
        )
        status_tag = (
            f" [{chat.auth_status}]"
            if chat.auth_status and chat.auth_status != "allowed"
            else ""
        )
        last = (chat.last_seen or "")[:16].replace("T", " ")
        lines.append(
            f"- {chat.chat_id} | {ctype} | {title}{handle}{members}{status_tag} "
            f"— last {last}"
        )
    return "\n".join(lines) + "\n"


def regenerate_l1_chats(
    instance_dir: Path,
    *,
    conn: sqlite3.Connection | None = None,
    limit: int | None = 50,
) -> Path | None:
    """Write `<instance>/memory/L1/CHATS.md` from the chats table.

    Skips if `memory/L1/` doesn't exist (instance not initialized yet).
    Returns the path written, or None on skip.
    """
    l1_dir = instance_dir / "memory" / "L1"
    if not l1_dir.is_dir():
        return None
    rows = list_chats(
        instance_dir, conn=conn, channel="telegram", limit=limit
    )
    target = _l1_chats_path(instance_dir)
    target.write_text(_format_l1_chats(rows), encoding="utf-8")
    return target


def _maybe_regenerate_l1_chats(instance_dir: Path) -> None:
    """Debounced regen — at most once per `_REGEN_DEBOUNCE_SECONDS` per instance.

    Failures are swallowed so chat-upsert never fails on a regen problem.
    """
    key = str(instance_dir.resolve())
    now = time.monotonic()
    with _REGEN_LOCK:
        last = _LAST_REGEN.get(key, 0.0)
        if now - last < _REGEN_DEBOUNCE_SECONDS:
            return
        _LAST_REGEN[key] = now
    try:
        regenerate_l1_chats(instance_dir)
    except Exception:  # noqa: BLE001
        pass


def prune_chats(
    instance_dir: Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    older_than_days: int,
    channel: str | None = None,
) -> int:
    """Delete chats whose `last_seen` is older than `older_than_days`.

    Returns the number of rows removed.
    """
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=int(older_than_days))
    cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    with _scoped_conn(instance_dir, conn) as c:
        if channel is not None:
            cur = c.execute(
                "DELETE FROM chats WHERE channel=? AND last_seen < ?",
                (channel, cutoff),
            )
        else:
            cur = c.execute("DELETE FROM chats WHERE last_seen < ?", (cutoff,))
        c.commit()
        return cur.rowcount
