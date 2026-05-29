"""Operator-driven session actions: Stop (Phase 1) and Background (Phase 2).

Stop semantics:
  1. Look up the action entry by ``session_id``.
  2. Mark the entry stopped so a second tap is a no-op.
  3. Send SIGTERM to the brain child's process group (the subprocess is
     launched with ``start_new_session=True`` in ``brains/base.py``, so its
     PGID == its PID).
  4. Poll for exit up to ``stop_grace_seconds`` (default 5).
  5. If still alive, escalate to SIGKILL.
  6. The dispatcher / slot worker observes the subprocess exit on its
     ``communicate()`` call, the ``finally`` block in ``Brain.invoke`` runs
     ``actions_registry.unregister``, and the natural slot-release path in
     ``_run_in_slot.finally`` frees the parallel slot.

Background semantics:
  1. Look up the action entry by ``session_id``.
  2. Check the per-chat cap (``max_background_per_chat``).
  3. Flip ``role`` to ``backgrounded``, snapshot the original card binding
     so the runtime can edit it on completion.
  4. The brain subprocess keeps running unaware. The inbound router treats
     the chat as having no primary, so a new inbound message spawns a fresh
     session immediately. The runtime intercepts the backgrounded session's
     final reply as a "Background done" completion card.
  5. Every action attempt is appended to ``state/actions.jsonl`` as a
     structured audit record.

Both handlers are idempotent — double tap returns ``already_*`` without
re-executing side effects.
"""

from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import actions_registry


@dataclass(frozen=True)
class StopResult:
    ok: bool
    already_stopped: bool
    elapsed_ms: int
    reason: str = ""


@dataclass(frozen=True)
class BackgroundResult:
    ok: bool
    already_backgrounded: bool = False
    capped: bool = False
    elapsed_ms: int = 0
    reason: str = ""


def stop_session(
    session_id: str,
    *,
    stop_grace_seconds: int = 5,
    instance_dir: Optional[Path] = None,
    actor_chat_id: str = "",
    sleep: callable = time.sleep,
    now: callable = time.monotonic,
) -> StopResult:
    """SIGTERM + grace + SIGKILL the brain child's process group. Idempotent."""
    start = now()
    entry = actions_registry.resolve_by_session(session_id)
    chat_id = entry.chat_id if entry is not None else ""
    if entry is None:
        result = StopResult(
            ok=False,
            already_stopped=False,
            elapsed_ms=0,
            reason="not_found",
        )
        _audit(instance_dir, session_id, chat_id, "stop", result, time.time(),
               actor_chat_id=actor_chat_id)
        return result
    if entry.stopped:
        result = StopResult(
            ok=True,
            already_stopped=True,
            elapsed_ms=int((now() - start) * 1000),
            reason="already_stopped",
        )
        _audit(instance_dir, session_id, chat_id, "stop", result, time.time(),
               actor_chat_id=actor_chat_id)
        return result

    actions_registry.mark_stopped(session_id)
    pid = entry.child_pid

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        result = StopResult(
            ok=True,
            already_stopped=True,
            elapsed_ms=int((now() - start) * 1000),
            reason="pid_gone",
        )
        _audit(instance_dir, session_id, chat_id, "stop", result, time.time(),
               actor_chat_id=actor_chat_id)
        return result
    except PermissionError as exc:  # pragma: no cover - defensive
        result = StopResult(
            ok=False,
            already_stopped=False,
            elapsed_ms=int((now() - start) * 1000),
            reason=f"permission: {exc}",
        )
        _audit(instance_dir, session_id, chat_id, "stop", result, time.time(),
               actor_chat_id=actor_chat_id)
        return result

    grace = max(0.0, float(stop_grace_seconds))
    deadline = now() + grace
    while now() < deadline:
        if not _pid_alive(pid):
            result = StopResult(
                ok=True,
                already_stopped=False,
                elapsed_ms=int((now() - start) * 1000),
                reason="sigterm",
            )
            _audit(instance_dir, session_id, chat_id, "stop", result, time.time(),
                   actor_chat_id=actor_chat_id)
            return result
        sleep(0.1)

    escalated = False
    try:
        os.killpg(pid, signal.SIGKILL)
        escalated = True
    except ProcessLookupError:
        pass
    except PermissionError:  # pragma: no cover - defensive
        pass

    result = StopResult(
        ok=True,
        already_stopped=False,
        elapsed_ms=int((now() - start) * 1000),
        reason="sigkill" if escalated else "sigterm_late",
    )
    _audit(instance_dir, session_id, chat_id, "stop", result, time.time(),
           actor_chat_id=actor_chat_id)
    return result


def background_session(
    session_id: str,
    *,
    chat_id: str,
    supervisor_msg_id: Optional[int] = None,
    max_per_chat: int = 3,
    instance_dir: Optional[Path] = None,
    actor_chat_id: str = "",
    now: callable = time.monotonic,
    wall_now: callable = time.time,
) -> BackgroundResult:
    """Demote a running session to background. Idempotent + cap-enforced.

    Args:
      session_id: action-registry session UUID for the running brain child.
      chat_id: Telegram chat_id the session is bound to. Required so the
        per-chat cap is enforced consistently regardless of which entry
        field stored it.
      supervisor_msg_id: Telegram message_id of the supervisor card to edit
        on completion. Snapshotted onto the entry as ``bg_supervisor_msg_id``.
      max_per_chat: refuse if already ``max_per_chat`` backgrounded sessions
        for this chat (default 3 from ``gateway.actions.max_background_per_chat``).
      instance_dir: when set, append the audit record to ``state/actions.jsonl``.

    Returns ``BackgroundResult``. Side effects:
      - registry entry's role → "backgrounded" (mark_backgrounded).
      - bg_supervisor_msg_id + bg_chat_id snapshotted on entry.
      - audit record written to ``state/actions.jsonl`` when instance_dir set.

    The brain subprocess is NOT signaled and never learns it was backgrounded
    — Background is a pure gateway-layer routing trick.
    """
    start = now()
    entry = actions_registry.resolve_by_session(session_id)
    if entry is None:
        result = BackgroundResult(
            ok=False,
            already_backgrounded=False,
            capped=False,
            elapsed_ms=0,
            reason="not_found",
        )
        _audit(instance_dir, session_id, chat_id, "background", result, wall_now(),
               actor_chat_id=actor_chat_id)
        return result
    if entry.role == "backgrounded":
        result = BackgroundResult(
            ok=True,
            already_backgrounded=True,
            capped=False,
            elapsed_ms=int((now() - start) * 1000),
            reason="already_backgrounded",
        )
        _audit(instance_dir, session_id, chat_id, "background", result, wall_now(),
               actor_chat_id=actor_chat_id)
        return result
    if actions_registry.count_backgrounded_for_chat(chat_id) >= max_per_chat:
        result = BackgroundResult(
            ok=False,
            already_backgrounded=False,
            capped=True,
            elapsed_ms=int((now() - start) * 1000),
            reason="cap_reached",
        )
        _audit(instance_dir, session_id, chat_id, "background", result, wall_now(),
               actor_chat_id=actor_chat_id)
        return result
    actions_registry.mark_backgrounded(
        session_id,
        bg_supervisor_msg_id=supervisor_msg_id,
        bg_chat_id=str(chat_id) if chat_id else "",
        when=wall_now(),
    )
    result = BackgroundResult(
        ok=True,
        already_backgrounded=False,
        capped=False,
        elapsed_ms=int((now() - start) * 1000),
        reason="backgrounded",
    )
    _audit(instance_dir, session_id, chat_id, "background", result, wall_now(),
           actor_chat_id=actor_chat_id)
    return result


def _audit(
    instance_dir: Optional[Path],
    session_id: str,
    chat_id: str,
    verb: str,
    result: object,
    ts: float,
    *,
    actor_chat_id: str = "",
) -> None:
    """Append a structured action-record line to ``state/actions.jsonl``.

    Record shape: {ts, session_id, chat_id, verb, actor_chat_id, result}.
    Best-effort: never raises, never blocks the caller on filesystem errors.
    """
    if instance_dir is None:
        return
    try:
        path = Path(instance_dir) / "state" / "actions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z"),
            "session_id": session_id,
            "chat_id": str(chat_id) if chat_id else "",
            "verb": verb,
            "actor_chat_id": str(actor_chat_id) if actor_chat_id else "",
            "result": _result_to_dict(result),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        pass


def audit_background_done(
    instance_dir: Optional[Path],
    session_id: str,
    chat_id: str,
    *,
    duration_s: float = 0.0,
    reason: str = "done",
) -> None:
    """Write a background_done record to the audit log.

    Called from runtime._handle_background_completion so the doctor check can
    correlate backgrounded sessions with their completion records.
    """
    _audit(
        instance_dir,
        session_id,
        chat_id,
        "background_done",
        {"duration_s": round(duration_s, 1), "reason": reason},
        time.time(),
    )


def _result_to_dict(result: object) -> dict:
    """Flatten a frozen dataclass or plain dict result to a JSON-safe mapping."""
    if isinstance(result, dict):
        return result
    if hasattr(result, "__dict__"):
        return dict(result.__dict__)
    try:
        from dataclasses import asdict
        return asdict(result)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return {"repr": repr(result)}


def _pid_alive(pid: int) -> bool:
    """True iff ``pid`` (the PG leader) is still alive."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - defensive
        return True
