"""Read and filter conversation corpus from gateway event queue."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from lib.gateway.queue import connect


@dataclass(frozen=True)
class Event:
    """Simplified event from queue for analysis."""
    id: int
    user_id: str
    conversation_id: str
    content: str
    response: str | None
    received_at: str


_PRIVACY_BLOCKLIST = [
    # Credentials
    r"sk[-_][A-Za-z0-9_]{16,}",  # OpenAI/Anthropic-style keys
    r"(AKIA|A3T)[A-Z0-9]{16}",  # AWS keys
    r"BEGIN RSA PRIVATE KEY",
    r"BEGIN OPENSSH PRIVATE KEY",
    # Explicit sexual content (conservative regex)
    r"(?:porn|xxx|nude|sex|fuck|cum|semen|ejacul|orgasm|masturbat)",
]

_COMPILED_BLOCKLIST = [re.compile(pattern, re.IGNORECASE) for pattern in _PRIVACY_BLOCKLIST]


def _passes_privacy_filter(text: str) -> bool:
    """Return True if text is safe, False if it should be filtered."""
    for pattern in _COMPILED_BLOCKLIST:
        if pattern.search(text):
            return False
    return True


def iter_events(
    instance_dir: Path,
    look_back_days: int = 7,
    user_id: str | None = None,
) -> Iterator[Event]:
    """Iterate events from queue.db, applying privacy filter.

    Only yields events where both content AND response pass privacy checks.
    If either is blocked, entire event is skipped (no partial redaction).
    """
    queue_db = instance_dir / "state" / "gateway" / "queue.db"
    if not queue_db.exists():
        return

    conn = sqlite3.connect(str(queue_db))
    conn.row_factory = sqlite3.Row
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=look_back_days)).isoformat()
        query = "SELECT id, user_id, conversation_id, content, response, received_at FROM events WHERE status = 'finished' AND received_at >= ?"
        params = [cutoff]
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        query += " ORDER BY received_at DESC"

        for row in conn.execute(query, params):
            # Privacy filter: both content AND response must be safe.
            if not _passes_privacy_filter(row["content"]):
                continue
            if row["response"] and not _passes_privacy_filter(row["response"]):
                continue

            yield Event(
                id=row["id"],
                user_id=row["user_id"],
                conversation_id=row["conversation_id"],
                content=row["content"],
                response=row["response"],
                received_at=row["received_at"],
            )
    finally:
        conn.close()


def count_events(instance_dir: Path, look_back_days: int = 7, user_id: str | None = None) -> int:
    """Count events in window (before privacy filtering)."""
    queue_db = instance_dir / "state" / "gateway" / "queue.db"
    if not queue_db.exists():
        return 0

    conn = sqlite3.connect(str(queue_db))
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=look_back_days)).isoformat()
        query = "SELECT COUNT(*) as cnt FROM events WHERE status = 'finished' AND received_at >= ?"
        params = [cutoff]
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        row = conn.execute(query, params).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()
