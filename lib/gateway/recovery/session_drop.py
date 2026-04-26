"""Sticky session_id clear + per-conversation lock.

Two events for the same `(channel, conversation_id, brain)` can fail
concurrently with `session_missing`. Without the lock, the second handler
would clear sticky a second time and re-enqueue, which would race the first
handler's redispatch. The per-conversation lock serializes the clear so the
second event sees the cleared sticky and short-circuits to a "racing" Defer.
"""

from __future__ import annotations

import threading
from pathlib import Path

from .. import queue, sessions


_LOCK_TABLE: dict[tuple[str, str, str], threading.Lock] = {}
_TABLE_LOCK = threading.Lock()


def _get_lock(channel: str, conversation_id: str, brain: str) -> threading.Lock:
    key = (channel, conversation_id, brain)
    with _TABLE_LOCK:
        lock = _LOCK_TABLE.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCK_TABLE[key] = lock
    return lock


def clear_sticky_session(
    instance_dir: Path,
    *,
    channel: str,
    conversation_id: str,
    brain: str,
    session_id: str | None = None,
) -> bool:
    """Clear the sessions table row for `(channel, conversation_id, brain)`.

    Returns True iff a row was actually deleted (i.e., we won the race against
    a concurrent handler). Callers use the return value to decide whether to
    re-enqueue (True) or back off as the racing branch (False).
    """
    lock = _get_lock(channel, conversation_id, brain)
    if not lock.acquire(blocking=False):
        return False
    try:
        conn = queue.connect(instance_dir)
        try:
            existing = sessions.get_session(
                conn,
                channel=channel,
                conversation_id=conversation_id,
                brain=brain,
            )
            if existing is None:
                return False
            if session_id and existing.session_id != session_id:
                # The sticky was already updated to a fresh session id by
                # another handler — we have nothing to clear.
                return False
            cur = conn.execute(
                "DELETE FROM sessions WHERE channel=? AND conversation_id=? AND brain=?",
                (channel, conversation_id, brain),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    finally:
        lock.release()
