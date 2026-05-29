"""In-memory action registry for supervisor card actions.

Bridge between three subsystems:
  - runtime / brain spawn: registers ``(session_id, child_pid, slot_id, ...)``
    when a brain subprocess starts; unregisters when it exits.
  - supervisor card delivery: looks up the active session for an event id,
    attaches the Telegram ``supervisor_msg_id`` after the card is sent.
  - telegram callback handler: resolves ``short_token`` taps to a session
    entry and forwards to ``actions.stop_session``.

Short token = first 12 chars of the action session UUID (hex). Collisions
within a single gateway lifetime are vanishingly unlikely (UUIDv4 birthday
bound at ~2^48); the registry is cleared on process exit anyway.

All operations are thread-safe — register/unregister run on the dispatch /
slot-worker threads while the Telegram poller reads + mutates concurrently.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ActionEntry:
    session_id: str
    short_token: str
    child_pid: int
    slot_id: int
    chat_id: str = ""
    conversation_id: str = ""
    event_id: Optional[int] = None
    supervisor_msg_id: Optional[int] = None
    card_text: str = ""
    started_at: float = field(default_factory=time.time)
    stopped: bool = False


_lock = threading.Lock()
_by_session: dict[str, ActionEntry] = {}
_by_token: dict[str, str] = {}
_by_event: dict[int, str] = {}


def short_token_for(session_id: str) -> str:
    """Return the 12-char short token for a session UUID (hex, no dashes)."""
    return session_id.replace("-", "")[:12]


def register(
    *,
    short_token: str,
    session_id: str,
    child_pid: int,
    slot_id: int,
    supervisor_msg_id: Optional[int] = None,
    chat_id: str = "",
    conversation_id: str = "",
    event_id: Optional[int] = None,
) -> None:
    """Register a freshly-spawned brain child. Idempotent on session_id."""
    entry = ActionEntry(
        session_id=session_id,
        short_token=short_token,
        child_pid=int(child_pid),
        slot_id=int(slot_id),
        chat_id=str(chat_id or ""),
        conversation_id=str(conversation_id or ""),
        event_id=int(event_id) if event_id is not None else None,
        supervisor_msg_id=int(supervisor_msg_id) if supervisor_msg_id is not None else None,
    )
    with _lock:
        _by_session[session_id] = entry
        _by_token[short_token] = session_id
        if entry.event_id is not None:
            _by_event[entry.event_id] = session_id


def resolve(short_token: str) -> Optional[ActionEntry]:
    """Look up an entry by 12-char short token, or None."""
    if not short_token:
        return None
    with _lock:
        sid = _by_token.get(short_token)
        if sid is None:
            return None
        return _by_session.get(sid)


def resolve_by_session(session_id: str) -> Optional[ActionEntry]:
    if not session_id:
        return None
    with _lock:
        return _by_session.get(session_id)


def resolve_by_event(event_id: int) -> Optional[ActionEntry]:
    if event_id is None:
        return None
    with _lock:
        sid = _by_event.get(int(event_id))
        if sid is None:
            return None
        return _by_session.get(sid)


def attach_supervisor_message_by_token(
    short_token: str, supervisor_msg_id: int, *, card_text: str = ""
) -> bool:
    """Bind a Telegram supervisor message_id (and last card text) to an entry."""
    if not short_token or supervisor_msg_id is None:
        return False
    with _lock:
        sid = _by_token.get(short_token)
        if sid is None:
            return False
        entry = _by_session.get(sid)
        if entry is None:
            return False
        entry.supervisor_msg_id = int(supervisor_msg_id)
        if card_text:
            entry.card_text = card_text
        return True


def mark_stopped(session_id: str) -> None:
    """Mark an entry as stopped so a second tap is a no-op."""
    with _lock:
        entry = _by_session.get(session_id)
        if entry is not None:
            entry.stopped = True


def unregister(session_id: str) -> None:
    """Drop the entry — called from base.py finally when the subprocess exits."""
    if not session_id:
        return
    with _lock:
        entry = _by_session.pop(session_id, None)
        if entry is None:
            return
        _by_token.pop(entry.short_token, None)
        if entry.event_id is not None:
            _by_event.pop(entry.event_id, None)


def snapshot() -> list[ActionEntry]:
    """Return a snapshot of current entries — diagnostics + tests."""
    with _lock:
        return list(_by_session.values())


def clear() -> None:
    """Reset registry — tests only."""
    with _lock:
        _by_session.clear()
        _by_token.clear()
        _by_event.clear()
