"""Operator-driven session actions: Stop (Phase 1) and Background (Phase 2).

Phase 1 implements ``stop_session`` only. ``background_session`` is a
placeholder that returns a ``not_implemented`` result; the Telegram callback
handler answers "Coming soon" without invoking it.

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

Idempotent — repeated calls return ``already_stopped=True`` without sending
new signals.
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass

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
    reason: str = ""


def stop_session(
    session_id: str,
    *,
    stop_grace_seconds: int = 5,
    sleep: callable = time.sleep,
    now: callable = time.monotonic,
) -> StopResult:
    """SIGTERM + grace + SIGKILL the brain child's process group. Idempotent."""
    start = now()
    entry = actions_registry.resolve_by_session(session_id)
    if entry is None:
        return StopResult(
            ok=False,
            already_stopped=False,
            elapsed_ms=0,
            reason="not_found",
        )
    if entry.stopped:
        return StopResult(
            ok=True,
            already_stopped=True,
            elapsed_ms=int((now() - start) * 1000),
            reason="already_stopped",
        )

    actions_registry.mark_stopped(session_id)
    pid = entry.child_pid

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return StopResult(
            ok=True,
            already_stopped=True,
            elapsed_ms=int((now() - start) * 1000),
            reason="pid_gone",
        )
    except PermissionError as exc:  # pragma: no cover - defensive
        return StopResult(
            ok=False,
            already_stopped=False,
            elapsed_ms=int((now() - start) * 1000),
            reason=f"permission: {exc}",
        )

    grace = max(0.0, float(stop_grace_seconds))
    deadline = now() + grace
    while now() < deadline:
        if not _pid_alive(pid):
            return StopResult(
                ok=True,
                already_stopped=False,
                elapsed_ms=int((now() - start) * 1000),
                reason="sigterm",
            )
        sleep(0.1)

    escalated = False
    try:
        os.killpg(pid, signal.SIGKILL)
        escalated = True
    except ProcessLookupError:
        pass
    except PermissionError:  # pragma: no cover - defensive
        pass

    return StopResult(
        ok=True,
        already_stopped=False,
        elapsed_ms=int((now() - start) * 1000),
        reason="sigkill" if escalated else "sigterm_late",
    )


def background_session(session_id: str) -> BackgroundResult:
    """Phase 2 stub. Phase 1 callers answer the callback_query with 'Coming soon'."""
    return BackgroundResult(ok=False, reason="not_implemented")


def _pid_alive(pid: int) -> bool:
    """True iff ``pid`` (the PG leader) is still alive."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - defensive
        return True
