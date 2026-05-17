"""Integration tests for the supervisor tick runner.

Critical regression: tick MUST NOT write to state/transcripts/.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gateway import queue
from supervisor.cards import Card
from supervisor.runner import CardSender, run_tick


# --- Fake sender for capturing card I/O ---

class FakeSender(CardSender):
    def __init__(self):
        self.sends: list[dict] = []
        self.edits: list[dict] = []
        self.deletes: list[dict] = []
        self.next_message_id = 1000

    def send(self, *, instance_dir, source, meta, card, log):
        mid = str(self.next_message_id)
        self.next_message_id += 1
        self.sends.append({
            "source": source,
            "meta": meta,
            "card": card,
            "message_id": mid,
        })
        return mid

    def edit(self, *, instance_dir, source, meta, message_id, card, log):
        self.edits.append({
            "source": source,
            "meta": meta,
            "message_id": message_id,
            "card": card,
        })
        return True

    def delete(self, *, instance_dir, source, meta, message_id, log):
        self.deletes.append({
            "source": source,
            "meta": meta,
            "message_id": message_id,
        })
        return True


# --- Fixtures ---

def _setup_instance(tmp_path: Path, *, enabled: bool = True) -> Path:
    ops = tmp_path / "ops"
    ops.mkdir()
    yaml = "supervisor:\n  enabled: " + ("true" if enabled else "false") + "\n"
    yaml += "  tick_interval_seconds: 0\n"  # disable throttle for tests
    yaml += "  min_card_interval_seconds: 0\n"
    (ops / "gateway.yaml").write_text(yaml)
    return tmp_path


def _make_running_event(
    instance_dir: Path,
    *,
    event_id_hint: int = 0,
    source: str = "telegram",
    chat_id: str = "12345",
    chat_type: str = "private",
    content: str = "audit Athena repo for auth issues",
    age_seconds: float = 60.0,
    brain: str = "claude",
    pid: int | None = None,
    stderr_tail: str = "Read(/foo/bar.py) done\n",
) -> int:
    """Insert an event already in 'running' status with old started_at."""
    conn = queue.connect(instance_dir)
    try:
        now = datetime.now(timezone.utc)
        started_at = datetime.fromtimestamp(now.timestamp() - age_seconds, timezone.utc).isoformat()
        meta = {
            "chat_id": chat_id,
            "chat_type": chat_type,
            "text": content,
            "message_id": 7777,
        }
        cur = conn.execute(
            """
            INSERT INTO events
              (source, content, meta, status, received_at, available_at, started_at,
               locked_by, locked_until, conversation_id)
            VALUES (?, ?, ?, 'running', ?, ?, ?, 'worker-1', ?, ?)
            """,
            (
                source, content, json.dumps(meta),
                started_at, started_at, started_at,
                started_at,
                chat_id,
            ),
        )
        conn.commit()
        event_id = cur.lastrowid
    finally:
        conn.close()

    # Write a gateway log entry so brain_map_from_log + pid_map_from_log find it
    log_path = queue.queue_dir(instance_dir) / "gateway.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    used_pid = pid if pid is not None else os.getpid()
    with log_path.open("a") as fh:
        fh.write(
            f"2026-05-17 adapter spawn event={event_id} brain={brain} pid={used_pid} "
            f"model=sonnet\n"
        )

    # Write the adapter stderr so the snapshot finds it
    stderr_dir = instance_dir / "state" / "gateway" / "adapter_stderr"
    stderr_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = stderr_dir / f"{event_id}-{used_pid}-1"
    stderr_path.write_text(stderr_tail)

    return event_id


def _mark_event_done(instance_dir: Path, event_id: int) -> None:
    conn = queue.connect(instance_dir)
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE events SET status='done', finished_at=? WHERE id=?",
            (now, event_id),
        )
        conn.commit()
    finally:
        conn.close()


# --- Tests ---

def test_disabled_returns_early(tmp_path):
    _setup_instance(tmp_path, enabled=False)
    sender = FakeSender()
    result = run_tick(tmp_path, sender=sender)
    assert result.enabled is False
    assert sender.sends == []


def test_no_running_events_no_cards(tmp_path):
    _setup_instance(tmp_path)
    sender = FakeSender()
    result = run_tick(tmp_path, sender=sender)
    assert result.enabled is True
    assert result.snapshots == []
    assert sender.sends == []


def test_running_event_past_threshold_sends_card(tmp_path):
    _setup_instance(tmp_path)
    eid = _make_running_event(tmp_path, age_seconds=120.0)
    sender = FakeSender()
    result = run_tick(tmp_path, sender=sender)
    assert len(result.snapshots) == 1
    assert len(sender.sends) == 1
    sent = sender.sends[0]
    assert sent["meta"]["chat_id"] == "12345"
    assert isinstance(sent["card"], Card)


def test_running_event_below_threshold_no_card(tmp_path):
    _setup_instance(tmp_path)
    _make_running_event(tmp_path, age_seconds=5.0)  # below claude threshold of 30s
    sender = FakeSender()
    result = run_tick(tmp_path, sender=sender)
    assert result.snapshots == []
    assert sender.sends == []


def test_second_tick_edits_existing_card(tmp_path):
    _setup_instance(tmp_path)
    _make_running_event(tmp_path, age_seconds=120.0, stderr_tail="Read(/x.py)\n")
    sender = FakeSender()

    run_tick(tmp_path, sender=sender)
    assert len(sender.sends) == 1
    assert len(sender.edits) == 0

    # Phase change so backoff doesn't skip
    # rewrite stderr to simulate new tool use
    stderr_dir = tmp_path / "state" / "gateway" / "adapter_stderr"
    for p in stderr_dir.glob("*"):
        p.write_text("Bash(ls)\n")

    run_tick(tmp_path, sender=sender)
    assert len(sender.sends) == 1
    assert len(sender.edits) == 1


def test_group_chat_skipped_by_default(tmp_path):
    _setup_instance(tmp_path)
    _make_running_event(tmp_path, age_seconds=120.0, chat_type="group")
    sender = FakeSender()
    result = run_tick(tmp_path, sender=sender)
    assert sender.sends == []
    assert any(s["reason"] == "group_chat" for s in result.skipped)


def test_voice_source_skipped_by_default(tmp_path):
    _setup_instance(tmp_path)
    _make_running_event(tmp_path, age_seconds=120.0, source="voice")
    sender = FakeSender()
    result = run_tick(tmp_path, sender=sender)
    assert sender.sends == []
    assert any("channel_disabled" in s["reason"] for s in result.skipped)


def test_worker_linked_skipped(tmp_path):
    _setup_instance(tmp_path)
    eid = _make_running_event(tmp_path, age_seconds=120.0, chat_id="conv-1")
    # Write a worker index pointing at the same conversation
    workers_dir = tmp_path / "state" / "workers"
    workers_dir.mkdir(parents=True)
    (workers_dir / "index.json").write_text(json.dumps([
        {"id": "worker-1", "status": "running", "conversation_id": "conv-1"},
    ]))
    sender = FakeSender()
    result = run_tick(tmp_path, sender=sender)
    assert sender.sends == []
    assert any(s["reason"] == "worker_linked" for s in result.skipped)


def test_completed_event_deletes_card(tmp_path):
    _setup_instance(tmp_path)
    eid = _make_running_event(tmp_path, age_seconds=120.0)
    sender = FakeSender()

    # Tick 1: send initial card
    run_tick(tmp_path, sender=sender)
    assert len(sender.sends) == 1
    initial_message_id = sender.sends[0]["message_id"]

    # Mark event done and tick again
    _mark_event_done(tmp_path, eid)
    run_tick(tmp_path, sender=sender)

    # Card should be deleted (not edited to ✅)
    assert len(sender.deletes) >= 1
    delete = sender.deletes[-1]
    assert delete["message_id"] == initial_message_id


# --- CRITICAL: loop guard ---

def test_tick_never_writes_to_transcripts(tmp_path):
    """LOOP GUARD: supervisor must not write to state/transcripts/.

    A supervisor card written to the transcript would make the brain's next
    turn think it generated that progress text itself, recursively narrating
    its own state. This is a hard invariant; if it ever fails, the design
    has regressed and must be fixed before merge.
    """
    _setup_instance(tmp_path)
    _make_running_event(tmp_path, age_seconds=120.0)

    transcripts_dir = tmp_path / "state" / "transcripts"

    sender = FakeSender()
    for _ in range(3):
        run_tick(tmp_path, sender=sender)

    # The directory may exist from gateway setup elsewhere, but no files inside
    if transcripts_dir.exists():
        files = list(transcripts_dir.rglob("*"))
        non_dirs = [f for f in files if f.is_file()]
        assert non_dirs == [], (
            f"Supervisor wrote to transcripts (loop guard violation): {non_dirs}"
        )

    # At least one card was sent (sanity that supervisor actually ran)
    assert len(sender.sends) >= 1


def test_card_count_increments_per_tick(tmp_path):
    _setup_instance(tmp_path)
    _make_running_event(tmp_path, age_seconds=120.0, stderr_tail="Read(x)\n")
    sender = FakeSender()

    run_tick(tmp_path, sender=sender)

    # Bump stderr to a different phase so backoff doesn't skip
    stderr_dir = tmp_path / "state" / "gateway" / "adapter_stderr"
    for p in stderr_dir.glob("*"):
        p.write_text("Bash(x)\n")
    run_tick(tmp_path, sender=sender)

    from supervisor.state import SupervisorState
    state = SupervisorState.load(tmp_path)
    # exactly one tracked event with card_count >= 2
    assert len(state.events) == 1
    ev_state = next(iter(state.events.values()))
    assert ev_state.card_count >= 2


# --- Phase 5: silent recovery integration ---

def test_dead_pid_triggers_silent_recovery(tmp_path):
    """Event with dead PID gets re-queued; no card sent that tick."""
    _setup_instance(tmp_path)
    # Use a definitely-dead PID — process 999999 won't exist in a fresh testbed
    eid = _make_running_event(tmp_path, age_seconds=120.0, pid=999999)
    sender = FakeSender()
    result = run_tick(tmp_path, sender=sender)

    # Recovery triggered → no card emitted
    assert len(sender.sends) == 0
    assert len(result.recoveries) == 1
    assert result.recoveries[0]["event_id"] == eid

    # Event status reset to 'queued'
    conn = queue.connect(tmp_path)
    try:
        row = conn.execute("SELECT status FROM events WHERE id=?", (eid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "queued"


def test_session_poison_drops_resume(tmp_path):
    """Session-poison stderr → drop resume_session from meta."""
    _setup_instance(tmp_path)
    eid = _make_running_event(
        tmp_path,
        age_seconds=120.0,
        pid=999999,
        stderr_tail="error: unknown variant `image_url`\n",
    )
    # Inject resume_session into meta
    conn = queue.connect(tmp_path)
    try:
        row = conn.execute("SELECT meta FROM events WHERE id=?", (eid,)).fetchone()
        meta = json.loads(row["meta"])
        meta["resume_session"] = "deadbeef-uuid"
        conn.execute("UPDATE events SET meta=? WHERE id=?", (json.dumps(meta), eid))
        conn.commit()
    finally:
        conn.close()

    sender = FakeSender()
    result = run_tick(tmp_path, sender=sender)

    assert len(result.recoveries) == 1
    assert result.recoveries[0]["class"] == "session_poison"
    assert result.recoveries[0]["drop_resume_session"] is True

    conn = queue.connect(tmp_path)
    try:
        row = conn.execute("SELECT meta FROM events WHERE id=?", (eid,)).fetchone()
    finally:
        conn.close()
    new_meta = json.loads(row["meta"])
    assert "resume_session" not in new_meta


def test_max_recovery_escalates_to_failed(tmp_path):
    """After max_recovery_attempts, supervisor escalates event to failed."""
    _setup_instance(tmp_path)
    from supervisor.state import EventState, SupervisorState
    eid = _make_running_event(tmp_path, age_seconds=120.0, pid=999999)
    state = SupervisorState()
    state.events[str(eid)] = EventState(recovery_attempts=2)
    state.save(tmp_path)

    sender = FakeSender()
    result = run_tick(tmp_path, sender=sender)

    # Escalation recorded in recoveries
    assert len(result.recoveries) == 1
    assert result.recoveries[0]["event_id"] == eid
    assert result.recoveries[0]["class"] == "escalated"

    # Event transitioned to failed
    conn = queue.connect(tmp_path)
    try:
        row = conn.execute("SELECT status, error FROM events WHERE id=?", (eid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "failed"
    assert row["error"] == "recovery_escalated"

    # State marks escalated=True
    from supervisor.state import SupervisorState
    state2 = SupervisorState.load(tmp_path)
    ev_state = state2.events.get(str(eid))
    assert ev_state is not None
    assert ev_state.escalated is True

    # No card sent for escalated event
    assert sender.sends == []


def test_edit_failure_forces_resend_in_same_tick(tmp_path):
    """Bug #8 / #9 — when edit returns False (e.g. Slack message_not_found,
    Telegram message deleted), the runner must clear the stale message_id and
    immediately send a fresh card so the user keeps seeing progress."""
    _setup_instance(tmp_path)
    _make_running_event(tmp_path, age_seconds=120.0, stderr_tail="Read(x)\n")

    class FailingEditSender(CardSender):
        def __init__(self):
            self.sends: list[str] = []
            self.edits: list[str] = []
            self.next_mid = 5000

        def send(self, *, instance_dir, source, meta, card, log):
            mid = str(self.next_mid)
            self.next_mid += 1
            self.sends.append(mid)
            return mid

        def edit(self, *, instance_dir, source, meta, message_id, card, log):
            self.edits.append(message_id)
            return False  # simulate message_not_found / 404

    sender = FailingEditSender()

    # Tick 1: send initial card
    run_tick(tmp_path, sender=sender)
    assert sender.sends == ["5000"]
    assert sender.edits == []

    # Phase change so backoff doesn't skip; rewrite stderr
    stderr_dir = tmp_path / "state" / "gateway" / "adapter_stderr"
    for p in stderr_dir.glob("*"):
        p.write_text("Bash(ls)\n")

    # Tick 2: edit fails → runner must clear id and send fresh
    run_tick(tmp_path, sender=sender)
    assert sender.edits == ["5000"]
    assert sender.sends == ["5000", "5001"], (
        "edit returned False but runner did not fall back to send (Bug #8/#9)"
    )

    # State should now carry the new id
    from supervisor.state import SupervisorState
    state = SupervisorState.load(tmp_path)
    ev_state = next(iter(state.events.values()))
    assert ev_state.channel_message_id == "5001"


def test_recovery_attempts_persist_across_requeue_cycle(tmp_path):
    """Bug #1 — recovery_attempts must survive the requeue→reclaim gap.

    Simulates a flapping adapter: each cycle the supervisor finds a dead-PID
    running event, recovers it (resets to queued), the dispatcher re-claims,
    the new adapter dies the same way, repeat. Before the fix, prune() would
    drop EventState as soon as the event left active_ids, so the counter
    reset to 0 every cycle and escalation never fired.
    """
    _setup_instance(tmp_path)
    sender = FakeSender()
    eid = _make_running_event(tmp_path, age_seconds=120.0, pid=999999)

    # Cycle 1: dead-PID → recover, counter goes 0→1.
    result = run_tick(tmp_path, sender=sender)
    assert len(result.recoveries) == 1
    assert result.recoveries[0]["attempt"] == 1

    # Dispatcher "re-claims": flip the row back to running with the same dead
    # PID. The state.json should still carry recovery_attempts=1.
    conn = queue.connect(tmp_path)
    try:
        conn.execute(
            "UPDATE events SET status='running', locked_by='worker-2', "
            "locked_until='2099-01-01T00:00:00Z' WHERE id=?",
            (eid,),
        )
        conn.commit()
    finally:
        conn.close()

    # Cycle 2: same dead PID → recover again, counter must go 1→2 (not 0→1).
    result2 = run_tick(tmp_path, sender=sender)
    assert len(result2.recoveries) == 1
    assert result2.recoveries[0]["attempt"] == 2, (
        "recovery_attempts reset to 0 across requeue cycle — Bug #1 regression"
    )

    # Cycle 3: same dead PID → recover again, counter 2→3 (still tracking).
    conn = queue.connect(tmp_path)
    try:
        conn.execute(
            "UPDATE events SET status='running', locked_by='worker-3', "
            "locked_until='2099-01-01T00:00:00Z' WHERE id=?",
            (eid,),
        )
        conn.commit()
    finally:
        conn.close()
    result3 = run_tick(tmp_path, sender=sender)
    # Default max_recovery_attempts=3 → this tick should escalate.
    assert len(result3.recoveries) == 1
    assert result3.recoveries[0]["class"] == "escalated"


def test_recovery_disabled_no_action(tmp_path):
    """When recovery.enabled=false, dead PIDs do not trigger reset."""
    ops = tmp_path / "ops"
    ops.mkdir()
    yaml = (
        "supervisor:\n"
        "  enabled: true\n"
        "  tick_interval_seconds: 0\n"
        "  min_card_interval_seconds: 0\n"
        "  recovery:\n"
        "    enabled: false\n"
    )
    (ops / "gateway.yaml").write_text(yaml)

    eid = _make_running_event(tmp_path, age_seconds=120.0, pid=999999)
    sender = FakeSender()
    result = run_tick(tmp_path, sender=sender)

    assert result.recoveries == []
    conn = queue.connect(tmp_path)
    try:
        row = conn.execute("SELECT status FROM events WHERE id=?", (eid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "running"
