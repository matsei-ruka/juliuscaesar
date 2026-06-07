"""Supervisor finalization must keep state until card cleanup succeeds."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue  # noqa: E402
from supervisor.runner import CardSender, _finalize_completed  # noqa: E402
from supervisor.state import EventState, SupervisorState  # noqa: E402


class _Sender(CardSender):
    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes = outcomes
        self.deletes: list[str] = []

    def send(self, *, instance_dir, source, meta, card, log):
        raise AssertionError("send should not be called")

    def edit(self, *, instance_dir, source, meta, message_id, card, log):
        raise AssertionError("edit should not be called")

    def delete(self, *, instance_dir, source, meta, message_id, log):
        self.deletes.append(message_id)
        return self.outcomes.pop(0)


def _insert_done_event(instance_dir: Path) -> int:
    conn = queue.connect(instance_dir)
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """
            INSERT INTO events
              (source, content, meta, status, received_at, available_at, started_at, finished_at,
               conversation_id)
            VALUES (?, ?, ?, 'done', ?, ?, ?, ?, ?)
            """,
            (
                "telegram",
                "done",
                json.dumps({"chat_id": "12345"}),
                now,
                now,
                now,
                now,
                "12345",
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_finalize_keeps_state_and_retries_when_delete_fails(tmp_path: Path) -> None:
    eid = _insert_done_event(tmp_path)
    state = SupervisorState(
        events={str(eid): EventState(channel_message_id="1081")}
    )
    now = datetime.now(timezone.utc)
    sender = _Sender([False, True])

    _finalize_completed(
        tmp_path,
        cfg=None,  # type: ignore[arg-type]
        state=state,
        active_ids=set(),
        now=now,
        dry_run=False,
        sender=sender,
        log=lambda _msg: None,
        reporter=None,
    )

    assert str(eid) in state.events
    assert state.events[str(eid)].channel_message_id == "1081"
    assert state.events[str(eid)].finalize_attempts == 1

    _finalize_completed(
        tmp_path,
        cfg=None,  # type: ignore[arg-type]
        state=state,
        active_ids=set(),
        now=now,
        dry_run=False,
        sender=sender,
        log=lambda _msg: None,
        reporter=None,
    )

    assert str(eid) not in state.events
    assert sender.deletes == ["1081", "1081"]


def test_finalize_abandons_after_attempt_cap(tmp_path: Path) -> None:
    eid = _insert_done_event(tmp_path)
    state = SupervisorState(
        events={str(eid): EventState(channel_message_id="1104", finalize_attempts=4)}
    )

    _finalize_completed(
        tmp_path,
        cfg=None,  # type: ignore[arg-type]
        state=state,
        active_ids=set(),
        now=datetime.now(timezone.utc),
        dry_run=False,
        sender=_Sender([False]),
        log=lambda _msg: None,
        reporter=None,
    )

    assert str(eid) not in state.events
    log_path = tmp_path / "state" / "logs" / "supervisor.jsonl"
    assert "finalize_abandoned" in log_path.read_text(encoding="utf-8")
