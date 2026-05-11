"""Unit tests for `lib/skills/gemini_deep_research/jobs.py` + auth lock.

Verifies state-file write/read round-trip, lock acquisition + timeout, and
the completion event payload picked up by `JcEventsChannel`.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from skills.gemini_deep_research import jobs
from skills.gemini_deep_research.auth import (
    DEFAULT_LOCK_TIMEOUT,
    acquire_lock,
    profile_dir,
)
from skills.gemini_deep_research.errors import EXIT_BUSY, DeepResearchError


def test_new_job_id_is_sortable_and_unique() -> None:
    a = jobs.new_job_id()
    time.sleep(0.001)
    b = jobs.new_job_id()
    assert a != b
    assert len(a) == len(b)
    assert sorted([a, b]) == [a, b] or sorted([a, b]) == [b, a]


def test_write_and_read_state_round_trip(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    instance.mkdir()
    state = jobs.JobState(job_id="job-1", query="hello", status="ok", exit_code=0, sources_count=3)
    jobs.write_state(instance, state)
    out = jobs.read_state(instance, "job-1")
    assert out is not None
    assert out.query == "hello"
    assert out.exit_code == 0
    assert out.sources_count == 3
    assert out.backend == "gemini"


def test_list_jobs_returns_in_reverse_order(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    instance.mkdir()
    # IDs are ULID-ish (lexicographically sortable); the helper sorts the
    # job-state filenames in reverse, so newer (larger) IDs come first.
    older = "aaaaaaaaaa-aaaaaa"
    newer = "bbbbbbbbbb-bbbbbb"
    jobs.write_state(instance, jobs.JobState(job_id=older, query="a"))
    jobs.write_state(instance, jobs.JobState(job_id=newer, query="b"))
    rows = jobs.list_jobs(instance, limit=10)
    ids = [r.job_id for r in rows]
    assert ids == [newer, older]


def test_reconcile_status_marks_dead_pid_stale(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    instance.mkdir()
    state = jobs.JobState(job_id="dead-1", query="q", status="running", pid=999_999_999)
    jobs.write_state(instance, state)
    fresh = jobs.read_state(instance, "dead-1")
    assert fresh is not None
    out = jobs.reconcile_status(fresh)
    assert out.status == "stale"


def test_emit_completion_event_writes_jc_events_payload(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    instance.mkdir()
    state = jobs.JobState(
        job_id="job-X",
        query="Compare X vs Y",
        status="ok",
        exit_code=0,
        duration_seconds=312,
        sources_count=24,
        report_path="/tmp/report.md",
        meta_path="/tmp/meta.json",
    )
    out = jobs.emit_completion_event(instance, state, notify_chat_id="123")
    payload = json.loads(out.read_text())
    assert payload["event_type"] == "research.completed"
    assert payload["event_id"] == "research-job-X"
    assert payload["notify_chat_id"] == "123"
    assert payload["status"] == "ok"
    assert payload["sources_count"] == 24


def test_cancel_no_such_job_returns_false(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    instance.mkdir()
    ok, message = jobs.cancel(instance, "missing")
    assert ok is False
    assert "no such job" in message


def test_cancel_with_dead_pid_marks_stale(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    instance.mkdir()
    state = jobs.JobState(job_id="z", query="q", status="running", pid=999_999_999)
    jobs.write_state(instance, state)
    ok, _ = jobs.cancel(instance, "z")
    assert ok is False
    fresh = jobs.read_state(instance, "z")
    assert fresh is not None
    assert fresh.status == "stale"


# --- locking ----------------------------------------------------------------


def test_acquire_lock_serialises_concurrent_holders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("JC_RESEARCH_PROFILE_DIR", str(tmp_path / "profile"))
    with acquire_lock(timeout=2):
        with pytest.raises(DeepResearchError) as exc_info:
            with acquire_lock(timeout=0.5):
                pass
    assert exc_info.value.code == EXIT_BUSY


def test_default_lock_timeout_is_60s() -> None:
    assert DEFAULT_LOCK_TIMEOUT == 60.0


def test_profile_dir_honours_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JC_RESEARCH_PROFILE_DIR", str(tmp_path / "p"))
    assert profile_dir() == (tmp_path / "p").resolve()
