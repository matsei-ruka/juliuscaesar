"""Supervisor silent recovery — Phase 5.

Detects crash classes for `running` events whose adapter PID is gone and
silently resets the event back to `queued` so the dispatcher can re-pick it
without operator intervention. The user only sees the card go back to
🟢 starting on the new attempt — never "crash" or "error".

Recovery classes:
  - crash_no_exit:   PID dead, no recognized stderr pattern.
  - session_poison:  stderr matches a known poison pattern → drop resume_session.
  - segfault:        stderr matches SIGSEGV / signal: killed → reset (no drop).
  - provider_5xx:    stderr matches HTTP 5xx → reset with 30s backoff.

Loop guard: per-event ``recovery_attempts`` capped at ``max_recovery_attempts``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from gateway import queue
from watchdog.registry import _parse_yaml

from .config import SupervisorConfig
from .models import EventSnapshot
from .state import EventState


LogFn = Callable[[str], None]


CLASS_CRASH_NO_EXIT = "crash_no_exit"
CLASS_SESSION_POISON = "session_poison"
CLASS_SEGFAULT = "segfault"
CLASS_PROVIDER_5XX = "provider_5xx"

_DEFAULT_PATTERNS: dict[str, list[str]] = {
    CLASS_SESSION_POISON: [
        r"unknown variant `image_url`",
        r"unknown variant 'image_url'",
        r'unknown variant "image_url"',
        r"invalid type: message_kind",
        r"Stream ended without complete response",
        r"session token expired",
    ],
    CLASS_SEGFAULT: [
        r"Segmentation fault",
        r"SIGSEGV",
        r"signal: killed",
    ],
    CLASS_PROVIDER_5XX: [
        r"HTTP 5\d{2}",
        r"status code 5\d{2}",
        r"status: 5\d{2}",
        r"upstream connect error",
        r"503 Service Unavailable",
        r"502 Bad Gateway",
        r"504 Gateway Timeout",
    ],
}


@dataclass(frozen=True)
class RecoveryDecision:
    """The action a supervisor tick takes for one snapshot."""
    triggered: bool
    failure_class: str = ""
    drop_resume_session: bool = False
    available_in_seconds: int = 0
    reason: str = ""


def needs_recovery(snap: EventSnapshot) -> bool:
    """A running event whose PID is gone is a recovery candidate."""
    return (
        snap.event.status == "running"
        and snap.adapter.pid is not None
        and snap.adapter.pid_alive is False
    )


def classify_failure(
    stderr_tail: str, patterns: dict[str, list[re.Pattern]] | None = None
) -> str:
    """Match stderr tail against poison/segfault/5xx patterns.

    Returns the failure class name or ``CLASS_CRASH_NO_EXIT`` if no pattern matches.
    """
    if not stderr_tail:
        return CLASS_CRASH_NO_EXIT
    compiled = patterns if patterns is not None else _compile_patterns(_DEFAULT_PATTERNS)
    for cls in (CLASS_SESSION_POISON, CLASS_SEGFAULT, CLASS_PROVIDER_5XX):
        for pat in compiled.get(cls, ()):
            if pat.search(stderr_tail):
                return cls
    return CLASS_CRASH_NO_EXIT


def decide(
    snap: EventSnapshot,
    ev_state: EventState,
    cfg: SupervisorConfig,
    *,
    patterns: dict[str, list[re.Pattern]] | None = None,
) -> RecoveryDecision:
    """Decide whether to recover this snapshot and how."""
    if not cfg.recovery_enabled:
        return RecoveryDecision(triggered=False, reason="recovery_disabled")
    if not needs_recovery(snap):
        return RecoveryDecision(triggered=False, reason="pid_alive_or_no_pid")
    if ev_state.recovery_attempts >= cfg.max_recovery_attempts:
        return RecoveryDecision(
            triggered=False, reason="max_recovery_attempts_exceeded"
        )

    failure_class = classify_failure(snap.adapter.stderr_tail, patterns)

    if failure_class == CLASS_SESSION_POISON:
        return RecoveryDecision(
            triggered=True,
            failure_class=failure_class,
            drop_resume_session=True,
        )
    if failure_class == CLASS_PROVIDER_5XX:
        return RecoveryDecision(
            triggered=True,
            failure_class=failure_class,
            available_in_seconds=30,
        )
    # segfault + crash_no_exit: simple reset, no drop, no backoff
    return RecoveryDecision(triggered=True, failure_class=failure_class)


def apply_recovery(
    instance_dir: Path,
    event_id: int,
    decision: RecoveryDecision,
    *,
    log: LogFn | None = None,
) -> bool:
    """Apply the queue reset implied by the decision. Returns True on success."""
    if not decision.triggered:
        return False
    conn = queue.connect(instance_dir)
    try:
        ok = queue.reset_running_to_queued(
            conn,
            event_id,
            drop_resume_session=decision.drop_resume_session,
            available_in_seconds=decision.available_in_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"supervisor recovery reset failed event={event_id}: {exc}")
        return False
    finally:
        conn.close()
    if log and ok:
        log(
            f"supervisor recovery event={event_id} class={decision.failure_class} "
            f"drop_resume={decision.drop_resume_session} "
            f"backoff={decision.available_in_seconds}s"
        )
    return ok


def load_patterns(
    instance_dir: Path, recovery_patterns_path: str = ""
) -> dict[str, list[re.Pattern]]:
    """Load recovery patterns from yaml. Falls back to defaults on any error."""
    if recovery_patterns_path:
        path = Path(recovery_patterns_path)
        if not path.is_absolute():
            path = instance_dir / path
    else:
        # Built-in patterns shipped with the supervisor module.
        path = Path(__file__).parent / "recovery_patterns.yaml"

    if not path.exists():
        return _compile_patterns(_DEFAULT_PATTERNS)
    try:
        data = _parse_yaml(path.read_text(encoding="utf-8"))
    except Exception:
        return _compile_patterns(_DEFAULT_PATTERNS)
    if not isinstance(data, dict):
        return _compile_patterns(_DEFAULT_PATTERNS)

    raw: dict[str, list[str]] = {}
    for cls, val in data.items():
        if isinstance(val, list):
            raw[str(cls)] = [str(v) for v in val if isinstance(v, str)]
    if not raw:
        return _compile_patterns(_DEFAULT_PATTERNS)
    return _compile_patterns(raw)


def _compile_patterns(raw: dict[str, list[str]]) -> dict[str, list[re.Pattern]]:
    compiled: dict[str, list[re.Pattern]] = {}
    for cls, patterns in raw.items():
        compiled[cls] = []
        for p in patterns:
            try:
                compiled[cls].append(re.compile(p, re.IGNORECASE))
            except re.error:
                continue
    return compiled
