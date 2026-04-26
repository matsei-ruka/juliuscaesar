"""Chat directory backed by the gateway queue DB.

Tracks every chat (DM, group, supergroup, channel) the gateway has seen on a
given channel. Rows are upserted on every inbound message. Reading the table
gives the brain awareness of the full chat universe, not only the conversation
that produced the active event.

The `channel` column is intentionally generic so a future Discord/Slack
adapter can write here too. Telegram is the only writer today.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import queue


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _row_to_chat(row: sqlite3.Row | None) -> Chat | None:
    if row is None:
        return None
    return Chat(**{key: row[key] for key in row.keys()})


def upsert_chat(
    instance_dir: Path,
    *,
    channel: str,
    chat_id: str,
    chat_type: str | None = None,
    title: str | None = None,
    username: str | None = None,
    member_count: int | None = None,
    last_message_id: str | None = None,
) -> Chat:
    """Insert or refresh a chat row.

    On conflict, `first_seen` is preserved, `last_seen` is bumped to now,
    and the optional fields (`chat_type`, `title`, `username`,
    `member_count`, `last_message_id`) are only overwritten when the
    new value is non-NULL — a transiently missing field in one update
    must not wipe a previously-known value.
    """

    ts = queue.now_iso()
    conn = queue.connect(instance_dir)
    try:
        conn.execute(
            """
            INSERT INTO chats(
                channel, chat_id, chat_type, title, username,
                member_count, first_seen, last_seen, last_message_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel, chat_id) DO UPDATE SET
                chat_type       = COALESCE(excluded.chat_type, chats.chat_type),
                title           = COALESCE(excluded.title, chats.title),
                username        = COALESCE(excluded.username, chats.username),
                member_count    = COALESCE(excluded.member_count, chats.member_count),
                last_seen       = excluded.last_seen,
                last_message_id = COALESCE(excluded.last_message_id, chats.last_message_id)
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
            ),
        )
        conn.commit()
        chat = _row_to_chat(
            conn.execute(
                "SELECT * FROM chats WHERE channel=? AND chat_id=?",
                (channel, chat_id),
            ).fetchone()
        )
    finally:
        conn.close()
    if chat is None:
        raise RuntimeError("failed to read upserted chat")
    return chat


def get_chat(
    instance_dir: Path,
    *,
    channel: str,
    chat_id: str,
) -> Chat | None:
    conn = queue.connect(instance_dir)
    try:
        return _row_to_chat(
            conn.execute(
                "SELECT * FROM chats WHERE channel=? AND chat_id=?",
                (channel, chat_id),
            ).fetchone()
        )
    finally:
        conn.close()


def list_chats(
    instance_dir: Path,
    *,
    channel: str | None = None,
    limit: int | None = None,
) -> list[Chat]:
    """Return chats ordered by `last_seen DESC`."""
    conn = queue.connect(instance_dir)
    try:
        params: list[Any] = []
        sql = "SELECT * FROM chats"
        if channel is not None:
            sql += " WHERE channel=?"
            params.append(channel)
        sql += " ORDER BY last_seen DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [chat for row in rows if (chat := _row_to_chat(row)) is not None]


def prune_chats(
    instance_dir: Path,
    *,
    older_than_days: int,
    channel: str | None = None,
) -> int:
    """Delete chats whose `last_seen` is older than `older_than_days`.

    Returns the number of rows removed.
    """
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=int(older_than_days))
    cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = queue.connect(instance_dir)
    try:
        if channel is not None:
            cur = conn.execute(
                "DELETE FROM chats WHERE channel=? AND last_seen < ?",
                (channel, cutoff),
            )
        else:
            cur = conn.execute("DELETE FROM chats WHERE last_seen < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
