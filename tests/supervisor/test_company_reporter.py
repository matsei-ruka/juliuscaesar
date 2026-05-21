"""Tests for the supervisor-driven the-company worker reporter.

Reuses the same fixtures as ``test_runner.py`` so a running event can be
inserted into the SQLite events table and the supervisor tick exercised
end-to-end. The reporter itself is mocked — these tests cover the wiring
between supervisor lifecycle and reporter calls, not the HTTP transport.
"""

from __future__ import annotations

import json
import os
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from company.supervisor_reporter import Reporter as CompanyReporter
from gateway import queue
from supervisor.runner import CardSender, run_tick
from supervisor.state import SupervisorState


# --- Fakes ---


class FakeReporter:
    """Records every call instead of POSTing — instance of the real Reporter
    is a structural-typing duck. The supervisor only calls
    ``report_started`` and ``report_finished``."""

    def __init__(self, *, started_ok: bool = True, finished_ok: bool = True):
        self.started_calls: list[dict[str, Any]] = []
        self.finished_calls: list[dict[str, Any]] = []
        self.started_ok = started_ok
        self.finished_ok = finished_ok

    def report_started(
        self, event_id, topic, started_at, brain, model,
    ) -> bool:
        self.started_calls.append(
            {
                "event_id": event_id,
                "topic": topic,
                "started_at": started_at,
                "brain": brain,
                "model": model,
            }
        )
        return self.started_ok

    def report_finished(
        self, event_id, started_at, finished_at, brain, model,
    ) -> bool:
        self.finished_calls.append(
            {
                "event_id": event_id,
                "started_at": started_at,
                "finished_at": finished_at,
                "brain": brain,
                "model": model,
            }
        )
        return self.finished_ok


class FakeSender(CardSender):
    def __init__(self):
        self.sends: list[dict] = []
        self.edits: list[dict] = []
        self.deletes: list[dict] = []
        self.next_message_id = 9000

    def send(self, *, instance_dir, source, meta, card, log):
        mid = str(self.next_message_id)
        self.next_message_id += 1
        self.sends.append({"source": source, "meta": meta, "card": card, "message_id": mid})
        return mid

    def edit(self, *, instance_dir, source, meta, message_id, card, log):
        self.edits.append({"source": source, "meta": meta, "message_id": message_id, "card": card})
        return True

    def delete(self, *, instance_dir, source, meta, message_id, log):
        self.deletes.append({"source": source, "meta": meta, "message_id": message_id})
        return True


# --- Fixtures (mirror test_runner.py) ---


def _setup_instance(tmp_path: Path) -> Path:
    ops = tmp_path / "ops"
    ops.mkdir()
    yaml = (
        "supervisor:\n"
        "  enabled: true\n"
        "  tick_interval_seconds: 0\n"
        "  min_card_interval_seconds: 0\n"
    )
    (ops / "gateway.yaml").write_text(yaml)
    return tmp_path


def _write_company_config(
    tmp_path: Path, *, enabled: bool, api_key: str = "test-key-abc"
) -> None:
    """Write ops/the_company.yaml + the api-key file."""
    ops = tmp_path / "ops"
    ops.mkdir(exist_ok=True)
    yaml = (
        "the_company:\n"
        f"  enabled: {'true' if enabled else 'false'}\n"
        "  api_url: http://127.0.0.1:9999\n"
        "  agent_id: 00000000-0000-0000-0000-000000000000\n"
        "  api_key_file: state/company/api-key\n"
    )
    (ops / "the_company.yaml").write_text(yaml)

    state_dir = tmp_path / "state" / "company"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "api-key").write_text(api_key)


def _make_running_event(
    instance_dir: Path,
    *,
    chat_id: str = "12345",
    content: str = "audit Athena repo for auth issues",
    age_seconds: float = 120.0,
    brain: str = "claude",
    pid: int | None = None,
    stderr_tail: str = "Read(/foo/bar.py)\n",
) -> int:
    """Insert a row already in 'running' status with old started_at.

    Matches the helper in ``test_runner.py``. Includes the gateway log line
    so brain_map / pid_map find the event.
    """
    conn = queue.connect(instance_dir)
    try:
        now = datetime.now(timezone.utc)
        started_at = datetime.fromtimestamp(
            now.timestamp() - age_seconds, timezone.utc
        ).isoformat()
        meta = {"chat_id": chat_id, "chat_type": "private", "text": content, "message_id": 7777}
        cur = conn.execute(
            """
            INSERT INTO events
              (source, content, meta, status, received_at, available_at, started_at,
               locked_by, locked_until, conversation_id)
            VALUES ('telegram', ?, ?, 'running', ?, ?, ?, 'worker-1', ?, ?)
            """,
            (content, json.dumps(meta), started_at, started_at, started_at, started_at, chat_id),
        )
        conn.commit()
        event_id = cur.lastrowid
    finally:
        conn.close()

    log_path = queue.queue_dir(instance_dir) / "gateway.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    used_pid = pid if pid is not None else os.getpid()
    with log_path.open("a") as fh:
        fh.write(
            f"2026-05-21 adapter spawn event={event_id} brain={brain} pid={used_pid} "
            f"model=sonnet\n"
        )

    stderr_dir = instance_dir / "state" / "gateway" / "adapter_stderr"
    stderr_dir.mkdir(parents=True, exist_ok=True)
    (stderr_dir / f"{event_id}-{used_pid}-1").write_text(stderr_tail)
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


def test_new_event_triggers_report_started(tmp_path):
    """A fresh running event past the notice threshold → exactly one
    report_started with the right event_id and a populated topic."""
    _setup_instance(tmp_path)
    eid = _make_running_event(tmp_path)

    reporter = FakeReporter()
    sender = FakeSender()
    result = run_tick(tmp_path, sender=sender, reporter=reporter)

    assert len(result.snapshots) == 1
    assert len(reporter.started_calls) == 1
    call = reporter.started_calls[0]
    assert call["event_id"] == eid
    # Topic falls back to event content / meta text when no narrator runs
    # (no OPENROUTER_API_KEY in test env → generate_title returns None).
    assert call["topic"] == "audit Athena repo for auth issues"
    assert call["brain"] == "claude"
    assert isinstance(call["started_at"], datetime)


def test_started_not_called_twice_for_same_event(tmp_path):
    """``company_reported_started`` short-circuits the second tick."""
    _setup_instance(tmp_path)
    _make_running_event(tmp_path, stderr_tail="Read(x)\n")

    reporter = FakeReporter()
    sender = FakeSender()

    run_tick(tmp_path, sender=sender, reporter=reporter)
    assert len(reporter.started_calls) == 1

    # Bump stderr so backoff doesn't suppress the second tick's card emit.
    # (We're testing the started-call short-circuit, not card emission.)
    stderr_dir = tmp_path / "state" / "gateway" / "adapter_stderr"
    for p in stderr_dir.glob("*"):
        p.write_text("Bash(ls)\n")

    run_tick(tmp_path, sender=sender, reporter=reporter)
    assert len(reporter.started_calls) == 1, (
        "report_started fired twice for the same event — short-circuit broken"
    )


def test_finalize_triggers_report_finished(tmp_path):
    """When the event status flips to done and falls out of active_ids,
    finalize fires report_finished with the cached started_at + brain."""
    _setup_instance(tmp_path)
    eid = _make_running_event(tmp_path)

    reporter = FakeReporter()
    sender = FakeSender()

    # Tick 1: announces started.
    run_tick(tmp_path, sender=sender, reporter=reporter)
    assert len(reporter.started_calls) == 1
    assert reporter.finished_calls == []

    # Status flips → tick 2 finalizes.
    _mark_event_done(tmp_path, eid)
    run_tick(tmp_path, sender=sender, reporter=reporter)

    assert len(reporter.finished_calls) == 1
    call = reporter.finished_calls[0]
    assert call["event_id"] == eid
    assert call["brain"] == "claude"
    assert isinstance(call["started_at"], datetime)
    assert isinstance(call["finished_at"], datetime)
    assert call["finished_at"] >= call["started_at"]

    # State entry should be dropped after finalize.
    state = SupervisorState.load(tmp_path)
    assert str(eid) not in state.events


def test_reporter_disabled_when_config_missing(tmp_path):
    """No ops/the_company.yaml → default reporter is None → no calls."""
    _setup_instance(tmp_path)
    _make_running_event(tmp_path)
    # Note: we explicitly DO NOT call _write_company_config — file absent.

    # Reset any cached reporter from prior tests on this tmp_path.
    from supervisor.runner import _REPORTER_CACHE

    _REPORTER_CACHE.pop(str(tmp_path.resolve()), None)

    sender = FakeSender()
    # No reporter kwarg → runner falls back to the default loader which
    # should return None when config is missing.
    run_tick(tmp_path, sender=sender)

    state = SupervisorState.load(tmp_path)
    # Exactly one event tracked, and started was never marked.
    ev_states = list(state.events.values())
    assert len(ev_states) == 1
    assert ev_states[0].company_reported_started is False


def test_reporter_disabled_when_enabled_false(tmp_path):
    """``the_company.enabled: false`` → default reporter is None → no calls."""
    _setup_instance(tmp_path)
    _write_company_config(tmp_path, enabled=False)
    _make_running_event(tmp_path)

    from supervisor.runner import _REPORTER_CACHE

    _REPORTER_CACHE.pop(str(tmp_path.resolve()), None)

    sender = FakeSender()
    run_tick(tmp_path, sender=sender)

    state = SupervisorState.load(tmp_path)
    ev_states = list(state.events.values())
    assert len(ev_states) == 1
    assert ev_states[0].company_reported_started is False


def test_reporter_failure_leaves_state_for_retry(tmp_path):
    """``report_started`` returning False must not mark the state flag, so
    the next tick retries. The tick must not raise."""
    _setup_instance(tmp_path)
    _make_running_event(tmp_path)

    reporter = FakeReporter(started_ok=False)
    sender = FakeSender()

    # First tick: started returns False.
    result = run_tick(tmp_path, sender=sender, reporter=reporter)
    assert result.enabled is True
    assert len(reporter.started_calls) == 1
    state = SupervisorState.load(tmp_path)
    ev = next(iter(state.events.values()))
    assert ev.company_reported_started is False, "False return should leave flag unset"

    # Second tick: retries (started_ok still False, but call count grows).
    # Bump stderr to dodge backoff.
    stderr_dir = tmp_path / "state" / "gateway" / "adapter_stderr"
    for p in stderr_dir.glob("*"):
        p.write_text("Bash(x)\n")
    run_tick(tmp_path, sender=sender, reporter=reporter)
    assert len(reporter.started_calls) == 2, "retry did not fire on next tick"


def test_reporter_explicit_none_disables(tmp_path):
    """Passing reporter=None explicitly disables — even if config would
    enable it. This is the test-isolation path."""
    _setup_instance(tmp_path)
    _write_company_config(tmp_path, enabled=True)  # would normally enable
    _make_running_event(tmp_path)

    sender = FakeSender()
    run_tick(tmp_path, sender=sender, reporter=None)

    state = SupervisorState.load(tmp_path)
    ev_states = list(state.events.values())
    assert len(ev_states) == 1
    assert ev_states[0].company_reported_started is False


# --- Transport-layer smoke (urllib raises are swallowed by Reporter) ---


def test_real_reporter_http_failure_returns_false(monkeypatch):
    """Sanity check on the real Reporter: a urllib failure must surface as
    ``False`` and never raise. This is the only test that touches the real
    class — the brief explicitly defers full transport coverage to the
    noah-bitwell dev integration, but a 'never raises' smoke is cheap."""
    logs: list[str] = []
    reporter = CompanyReporter(
        api_url="http://127.0.0.1:1",  # closed port
        agent_id="agent-1",
        api_key="key-1",
        instance_boot_id="boot-1",
        log_fn=logs.append,
    )

    def _raise(*args, **kwargs):
        raise urllib.error.URLError("simulated connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise)

    ok = reporter.report_started(
        event_id=1,
        topic="t",
        started_at=datetime.now(timezone.utc),
        brain="claude",
        model="sonnet",
    )
    assert ok is False
    assert any("company reporter" in m for m in logs)
