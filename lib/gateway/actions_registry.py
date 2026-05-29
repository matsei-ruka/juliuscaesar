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

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
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
    # Phase 2: session role layer. "primary" is the live session bound to the
    # chat; "backgrounded" sessions keep running but their output is
    # intercepted as a completion notification and new chat traffic spawns a
    # fresh primary instead of resuming the backgrounded brain.
    role: str = "primary"
    bg_supervisor_msg_id: Optional[int] = None
    bg_chat_id: str = ""
    backgrounded_at: float = 0.0
    buffered_tool_messages: list[str] = field(default_factory=list)


_lock = threading.Lock()
_by_session: dict[str, ActionEntry] = {}
_by_token: dict[str, str] = {}
_by_event: dict[int, str] = {}
# Debounce map: session_id → monotonic timestamp of the last action press.
# Prevents double-tap races when a user hammers Stop/Background quickly.
_last_action_ts: dict[str, float] = {}


# --- Disk persistence (cross-process visibility) ---------------------------
#
# `actions_registry` is in-process state, but two distinct processes need to
# read the same entry:
#   - gateway PID: registers entries when spawning brain children, resolves
#     callback tokens, marks stopped/backgrounded.
#   - supervisor PID: rendering a supervisor card needs the entry's
#     short_token (looked up by event_id) so the card carries the inline
#     keyboard. Without disk persistence the supervisor's in-memory map is
#     always empty and no buttons ever appear.
#
# Shadow each in-mem entry to ``<instance_dir>/state/actions/event-<id>.json``.
# Cheap one-shot JSON write; cleanup on unregister. The supervisor's
# ``_actions_short_token`` reads the file when in-mem misses.


def _event_file(instance_dir: Path, event_id: int) -> Path:
    return Path(instance_dir) / "state" / "actions" / f"event-{int(event_id)}.json"


def _persist_entry(instance_dir: Optional[Path], entry: ActionEntry) -> None:
    if instance_dir is None or entry.event_id is None:
        return
    try:
        path = _event_file(instance_dir, entry.event_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": entry.session_id,
            "short_token": entry.short_token,
            "child_pid": entry.child_pid,
            "slot_id": entry.slot_id,
            "chat_id": entry.chat_id,
            "conversation_id": entry.conversation_id,
            "event_id": entry.event_id,
            "supervisor_msg_id": entry.supervisor_msg_id,
            "started_at": entry.started_at,
            "stopped": entry.stopped,
            "role": entry.role,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _remove_entry_file(instance_dir: Optional[Path], event_id: Optional[int]) -> None:
    if instance_dir is None or event_id is None:
        return
    try:
        _event_file(instance_dir, event_id).unlink(missing_ok=True)
    except OSError:
        pass


def _load_entry_from_disk(instance_dir: Path, event_id: int) -> Optional[ActionEntry]:
    path = _event_file(instance_dir, event_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return ActionEntry(
        session_id=data.get("session_id", ""),
        short_token=data.get("short_token", ""),
        child_pid=int(data.get("child_pid") or 0),
        slot_id=int(data.get("slot_id") or 0),
        chat_id=str(data.get("chat_id") or ""),
        conversation_id=str(data.get("conversation_id") or ""),
        event_id=int(data["event_id"]) if data.get("event_id") is not None else None,
        supervisor_msg_id=(
            int(data["supervisor_msg_id"])
            if data.get("supervisor_msg_id") is not None else None
        ),
        started_at=float(data.get("started_at") or 0.0),
        stopped=bool(data.get("stopped") or False),
        role=str(data.get("role") or "primary"),
    )


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
    instance_dir: Optional[Path] = None,
) -> None:
    """Register a freshly-spawned brain child. Idempotent on session_id.

    When ``instance_dir`` is set, also persists the entry to
    ``state/actions/event-<event_id>.json`` so the supervisor process (a
    different PID) can read it.
    """
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
    _persist_entry(instance_dir, entry)


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


def mark_stopped(session_id: str, *, instance_dir: Optional[Path] = None) -> None:
    """Mark an entry as stopped so a second tap is a no-op."""
    with _lock:
        entry = _by_session.get(session_id)
        if entry is not None:
            entry.stopped = True
    if entry is not None:
        _persist_entry(instance_dir, entry)


def mark_backgrounded(
    session_id: str,
    *,
    bg_supervisor_msg_id: Optional[int] = None,
    bg_chat_id: str = "",
    when: Optional[float] = None,
    instance_dir: Optional[Path] = None,
) -> bool:
    """Demote an entry to ``role='backgrounded'`` and snapshot card binding.

    Returns True iff the entry transitioned ``primary → backgrounded`` (i.e.
    not already backgrounded). Caller is responsible for the cap check —
    ``count_backgrounded_for_chat`` exposes the live count.
    """
    with _lock:
        entry = _by_session.get(session_id)
        if entry is None:
            return False
        if entry.role == "backgrounded":
            return False
        entry.role = "backgrounded"
        if bg_supervisor_msg_id is not None:
            entry.bg_supervisor_msg_id = int(bg_supervisor_msg_id)
        if bg_chat_id:
            entry.bg_chat_id = str(bg_chat_id)
        entry.backgrounded_at = float(when if when is not None else time.time())
    _persist_entry(instance_dir, entry)
    return True


def get_primary(chat_id: str) -> Optional[ActionEntry]:
    """Return the live ``role='primary'`` entry bound to ``chat_id``, or None.

    A chat has at most one primary at any given moment under serial dispatch;
    parallel slots may transiently overlap, in which case the first match is
    returned. Backgrounded entries are intentionally invisible to this lookup
    so the inbound router spawns fresh rather than queueing behind them.
    """
    if not chat_id:
        return None
    chat_id = str(chat_id)
    with _lock:
        for entry in _by_session.values():
            if entry.role == "primary" and entry.chat_id == chat_id:
                return entry
    return None


def get_backgrounded_by_chat_id(chat_id: str) -> list[ActionEntry]:
    """Return every ``role='backgrounded'`` entry bound to ``chat_id``."""
    if not chat_id:
        return []
    chat_id = str(chat_id)
    with _lock:
        return [
            entry for entry in _by_session.values()
            if entry.role == "backgrounded" and (
                entry.chat_id == chat_id or entry.bg_chat_id == chat_id
            )
        ]


def count_backgrounded_for_chat(chat_id: str) -> int:
    """Cap-check helper: how many backgrounded sessions exist for ``chat_id``."""
    return len(get_backgrounded_by_chat_id(chat_id))


def has_backgrounded_for_conversation(conversation_id: str) -> bool:
    """True iff any backgrounded session is bound to ``conversation_id``.

    Used by the inbound router to decide whether a fresh primary should be
    spawned instead of resuming the backgrounded brain's native session.
    """
    if not conversation_id:
        return False
    conversation_id = str(conversation_id)
    with _lock:
        return any(
            entry.role == "backgrounded" and entry.conversation_id == conversation_id
            for entry in _by_session.values()
        )


def is_backgrounded(session_id: str) -> bool:
    """True iff ``session_id`` is currently registered with ``role='backgrounded'``."""
    if not session_id:
        return False
    with _lock:
        entry = _by_session.get(session_id)
        return entry is not None and entry.role == "backgrounded"


def buffer_tool_message(session_id: str, text: str) -> bool:
    """Append ``text`` to the entry's buffered_tool_messages. Returns True on hit."""
    if not session_id or not text:
        return False
    with _lock:
        entry = _by_session.get(session_id)
        if entry is None:
            return False
        entry.buffered_tool_messages.append(str(text))
        return True


def snapshot_backgrounded_state(session_id: str) -> Optional[dict]:
    """Return a snapshot of fields the runtime needs at completion-card time.

    Captured before ``unregister`` so the runtime can render the "Background
    done" card after the brain subprocess exits.
    """
    with _lock:
        entry = _by_session.get(session_id)
        if entry is None:
            return None
        return {
            "role": entry.role,
            "chat_id": entry.chat_id,
            "bg_chat_id": entry.bg_chat_id,
            "bg_supervisor_msg_id": entry.bg_supervisor_msg_id,
            "supervisor_msg_id": entry.supervisor_msg_id,
            "card_text": entry.card_text,
            "started_at": entry.started_at,
            "backgrounded_at": entry.backgrounded_at,
            "buffered_tool_messages": list(entry.buffered_tool_messages),
        }


def unregister(session_id: str, *, instance_dir: Optional[Path] = None) -> None:
    """Drop the entry — called from base.py finally when the subprocess exits."""
    if not session_id:
        return
    event_id_to_remove: Optional[int] = None
    with _lock:
        entry = _by_session.pop(session_id, None)
        if entry is None:
            return
        _by_token.pop(entry.short_token, None)
        if entry.event_id is not None:
            _by_event.pop(entry.event_id, None)
            event_id_to_remove = entry.event_id
        _last_action_ts.pop(session_id, None)
    _remove_entry_file(instance_dir, event_id_to_remove)


def resolve_by_event_with_disk(
    instance_dir: Optional[Path], event_id: int
) -> Optional[ActionEntry]:
    """Like ``resolve_by_event`` but also consults the on-disk shadow.

    Lets the supervisor process (separate PID from gateway) read entries
    registered by the gateway. Disk fallback only when in-memory misses.
    """
    in_mem = resolve_by_event(event_id)
    if in_mem is not None:
        return in_mem
    if instance_dir is None or event_id is None:
        return None
    return _load_entry_from_disk(Path(instance_dir), int(event_id))


def snapshot() -> list[ActionEntry]:
    """Return a snapshot of current entries — diagnostics + tests."""
    with _lock:
        return list(_by_session.values())


def check_and_set_debounce(session_id: str, window_seconds: float = 2.0) -> bool:
    """Debounce guard for Stop/Background button presses.

    Returns True (= "ignore this press") if an action on ``session_id`` was
    recorded within ``window_seconds``. Otherwise records the current timestamp
    and returns False (= "proceed").

    Thread-safe; uses the shared registry lock so the check-then-set is atomic.
    """
    if not session_id:
        return False
    now = time.monotonic()
    with _lock:
        last = _last_action_ts.get(session_id, 0.0)
        if now - last < window_seconds:
            return True
        _last_action_ts[session_id] = now
        return False


def clear() -> None:
    """Reset registry — tests only."""
    with _lock:
        _by_session.clear()
        _by_token.clear()
        _by_event.clear()
        _last_action_ts.clear()
