"""Tests for the supervisor silent recovery module (Phase 5)."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gateway import queue
from supervisor.config import SupervisorConfig
from supervisor.models import AdapterInfo, EventSnapshot, PhaseResult
from supervisor.recovery import (
    CLASS_CRASH_NO_EXIT,
    CLASS_PROVIDER_5XX,
    CLASS_SEGFAULT,
    CLASS_SESSION_POISON,
    apply_recovery,
    classify_failure,
    decide,
    load_patterns,
    needs_recovery,
)
from supervisor.state import EventState


# --- fixtures -------------------------------------------------------------

def _phase():
    return PhaseResult(phase="reading", emoji="📖", label={"en": "reading", "it": "lettura"})


def _adapter(pid=None, pid_alive=True, stderr_tail=""):
    return AdapterInfo(
        event_id=1,
        pid=pid,
        stderr_path=None,
        stderr_mtime=None,
        stderr_tail=stderr_tail,
        pid_alive=pid_alive,
    )


def _event(eid=1, status="running"):
    return queue.Event(
        id=eid,
        source="telegram",
        source_message_id="42",
        user_id="100",
        conversation_id="conv",
        content="audit",
        meta=json.dumps({"chat_id": "12345"}),
        status=status,
        received_at="2026-05-17T10:00:00+00:00",
        available_at="2026-05-17T10:00:00+00:00",
        locked_by=None,
        locked_until=None,
        started_at="2026-05-17T10:00:00+00:00",
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


def _snap(pid=12345, pid_alive=False, stderr_tail="", status="running"):
    return EventSnapshot(
        event=_event(status=status),
        meta={"chat_id": "12345"},
        age_seconds=90.0,
        brain="claude",
        model="sonnet",
        adapter=_adapter(pid=pid, pid_alive=pid_alive, stderr_tail=stderr_tail),
        phase=_phase(),
        worker_linked=False,
        language="en",
    )


# --- needs_recovery -------------------------------------------------------

def test_needs_recovery_dead_pid():
    snap = _snap(pid=12345, pid_alive=False)
    assert needs_recovery(snap) is True


def test_needs_recovery_pid_alive():
    snap = _snap(pid=12345, pid_alive=True)
    assert needs_recovery(snap) is False


def test_needs_recovery_no_pid_recorded():
    snap = _snap(pid=None, pid_alive=False)
    assert needs_recovery(snap) is False


def test_needs_recovery_not_running():
    snap = _snap(pid=12345, pid_alive=False, status="done")
    assert needs_recovery(snap) is False


# --- classify_failure -----------------------------------------------------

def test_classify_session_poison_image_url():
    assert classify_failure("error: unknown variant `image_url`") == CLASS_SESSION_POISON


def test_classify_session_poison_quoted_variant():
    assert classify_failure("unknown variant 'image_url' encountered") == CLASS_SESSION_POISON


def test_classify_segfault():
    assert classify_failure("worker received SIGSEGV") == CLASS_SEGFAULT


def test_classify_provider_5xx():
    assert classify_failure("upstream returned HTTP 502 Bad Gateway") == CLASS_PROVIDER_5XX


def test_classify_no_match_returns_crash_no_exit():
    assert classify_failure("Read(/foo) ok") == CLASS_CRASH_NO_EXIT


def test_classify_empty_returns_crash_no_exit():
    assert classify_failure("") == CLASS_CRASH_NO_EXIT


def test_classify_case_insensitive():
    assert classify_failure("SEGMENTATION FAULT in worker") == CLASS_SEGFAULT


# --- decide ---------------------------------------------------------------

def _cfg(recovery_enabled=True, max_recovery_attempts=2):
    return SupervisorConfig(
        recovery_enabled=recovery_enabled,
        max_recovery_attempts=max_recovery_attempts,
    )


def test_decide_disabled():
    snap = _snap(pid_alive=False)
    decision = decide(snap, EventState(), _cfg(recovery_enabled=False))
    assert decision.triggered is False
    assert "disabled" in decision.reason


def test_decide_alive_pid_no_recovery():
    snap = _snap(pid_alive=True)
    decision = decide(snap, EventState(), _cfg())
    assert decision.triggered is False


def test_decide_max_attempts_exceeded():
    snap = _snap(pid_alive=False)
    ev = EventState(recovery_attempts=2)
    decision = decide(snap, ev, _cfg(max_recovery_attempts=2))
    assert decision.triggered is False
    assert "max_recovery_attempts_exceeded" in decision.reason


def test_decide_crash_no_exit_triggers_reset():
    snap = _snap(pid_alive=False, stderr_tail="Read(/foo.py) done")
    decision = decide(snap, EventState(), _cfg())
    assert decision.triggered is True
    assert decision.failure_class == CLASS_CRASH_NO_EXIT
    assert decision.drop_resume_session is False
    assert decision.available_in_seconds == 0


def test_decide_session_poison_drops_resume():
    snap = _snap(pid_alive=False, stderr_tail="unknown variant `image_url`")
    decision = decide(snap, EventState(), _cfg())
    assert decision.triggered is True
    assert decision.failure_class == CLASS_SESSION_POISON
    assert decision.drop_resume_session is True


def test_decide_provider_5xx_adds_backoff():
    snap = _snap(pid_alive=False, stderr_tail="HTTP 503 Service Unavailable")
    decision = decide(snap, EventState(), _cfg())
    assert decision.triggered is True
    assert decision.failure_class == CLASS_PROVIDER_5XX
    assert decision.available_in_seconds == 30


def test_decide_segfault_simple_reset():
    snap = _snap(pid_alive=False, stderr_tail="signal: killed (SIGSEGV)")
    decision = decide(snap, EventState(), _cfg())
    assert decision.triggered is True
    assert decision.failure_class == CLASS_SEGFAULT
    assert decision.drop_resume_session is False


# --- apply_recovery (integration with queue) ------------------------------

def _setup_queue(tmp_path: Path, *, resume_session: str | None = None) -> int:
    """Insert a 'running' event and return its id."""
    conn = queue.connect(tmp_path)
    try:
        meta = {"chat_id": "12345"}
        if resume_session:
            meta["resume_session"] = resume_session
        cur = conn.execute(
            """
            INSERT INTO events
              (source, content, meta, status, received_at, available_at, started_at,
               locked_by, locked_until)
            VALUES ('telegram', 'audit', ?, 'running',
                    '2026-05-17T10:00:00+00:00', '2026-05-17T10:00:00+00:00',
                    '2026-05-17T10:00:00+00:00', 'worker-1',
                    '2026-05-17T10:05:00+00:00')
            """,
            (json.dumps(meta),),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_apply_recovery_resets_to_queued(tmp_path):
    eid = _setup_queue(tmp_path)
    from supervisor.recovery import RecoveryDecision
    decision = RecoveryDecision(triggered=True, failure_class=CLASS_CRASH_NO_EXIT)
    ok = apply_recovery(tmp_path, eid, decision)
    assert ok is True
    conn = queue.connect(tmp_path)
    try:
        row = conn.execute("SELECT status, locked_by FROM events WHERE id=?", (eid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "queued"
    assert row["locked_by"] is None


def test_apply_recovery_drops_resume_session(tmp_path):
    eid = _setup_queue(tmp_path, resume_session="abc-uuid-123")
    from supervisor.recovery import RecoveryDecision
    decision = RecoveryDecision(
        triggered=True,
        failure_class=CLASS_SESSION_POISON,
        drop_resume_session=True,
    )
    ok = apply_recovery(tmp_path, eid, decision)
    assert ok is True
    conn = queue.connect(tmp_path)
    try:
        row = conn.execute("SELECT meta FROM events WHERE id=?", (eid,)).fetchone()
    finally:
        conn.close()
    meta = json.loads(row["meta"])
    assert "resume_session" not in meta


def test_apply_recovery_skips_when_not_running(tmp_path):
    """If status flipped to done/failed concurrently, reset must be a no-op."""
    eid = _setup_queue(tmp_path)
    conn = queue.connect(tmp_path)
    try:
        conn.execute("UPDATE events SET status='done' WHERE id=?", (eid,))
        conn.commit()
    finally:
        conn.close()
    from supervisor.recovery import RecoveryDecision
    decision = RecoveryDecision(triggered=True, failure_class=CLASS_CRASH_NO_EXIT)
    ok = apply_recovery(tmp_path, eid, decision)
    assert ok is False


def test_apply_recovery_no_op_if_not_triggered(tmp_path):
    eid = _setup_queue(tmp_path)
    from supervisor.recovery import RecoveryDecision
    decision = RecoveryDecision(triggered=False, reason="recovery_disabled")
    ok = apply_recovery(tmp_path, eid, decision)
    assert ok is False
    conn = queue.connect(tmp_path)
    try:
        row = conn.execute("SELECT status FROM events WHERE id=?", (eid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "running"


# --- load_patterns --------------------------------------------------------

def test_load_patterns_defaults_when_no_path(tmp_path):
    patterns = load_patterns(tmp_path, "")
    assert CLASS_SESSION_POISON in patterns
    assert CLASS_SEGFAULT in patterns
    assert CLASS_PROVIDER_5XX in patterns


def test_load_patterns_from_explicit_file(tmp_path):
    yml = tmp_path / "patterns.yaml"
    yml.write_text("session_poison:\n  - 'totally_custom_pattern'\n")
    patterns = load_patterns(tmp_path, "patterns.yaml")
    poison = patterns.get(CLASS_SESSION_POISON, [])
    assert any("totally_custom" in p.pattern for p in poison)


def test_load_patterns_invalid_yaml_falls_back(tmp_path):
    yml = tmp_path / "bad.yaml"
    yml.write_text("not: { valid yaml [")
    patterns = load_patterns(tmp_path, "bad.yaml")
    # Should fall back to defaults silently
    assert CLASS_SESSION_POISON in patterns
