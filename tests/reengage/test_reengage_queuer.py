from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from commitments.engine import add_commitment  # noqa: E402
from commitments.schema import Commitment, load, parse_datetime  # noqa: E402
from gateway import transcripts  # noqa: E402
from reengage.queuer import cancel_if_tracked, run  # noqa: E402


def _write_cfg(instance: Path, *, enabled: bool = True) -> None:
    (instance / "ops").mkdir(parents=True)
    data = {
        "enabled": enabled,
        "silence_threshold_hours": 48,
        "max_touches": 4,
        "touch_schedule": [48, 72, 96, 120],
        "allowed_slots": ["07:00", "19:00"],
        "quiet_hours": {"start": "23:00", "end": "07:00"},
        "tracked_chats": [
            {
                "chat_id": 123,
                "name": "Owner",
                "templates": {
                    "touch_1": "templates/re-engagement/touch-1.md",
                    "touch_2": "templates/re-engagement/touch-2.md",
                },
            }
        ],
    }
    (instance / "ops" / "reengage.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _write_template(instance: Path, name: str = "touch-1.md", text: str = "Checking in.") -> None:
    path = instance / "memory" / "L2" / "templates" / "re-engagement" / name
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")


def _append_user(instance: Path, ts: str) -> None:
    transcripts.append(
        instance,
        conversation_id="123",
        role="user",
        text="hello",
        channel="telegram",
        chat_id="123",
        ts=ts,
    )


def test_disabled_config_skips(tmp_path: Path) -> None:
    _write_cfg(tmp_path, enabled=False)
    summary = run(tmp_path)
    assert summary.skipped == ["disabled"]


def test_queues_first_touch_after_silence(tmp_path: Path) -> None:
    _write_cfg(tmp_path)
    _write_template(tmp_path)
    _append_user(tmp_path, "2026-05-09T06:00:00+00:00")

    summary = run(tmp_path, now=datetime(2026, 5, 11, 8, tzinfo=timezone.utc))

    assert summary.ok
    assert len(summary.queued) == 1
    path = next((tmp_path / "state" / "commitments").glob("*.yaml"))
    commitment = load(path)
    assert commitment.action == "telegram-send"
    assert commitment.chat_id == 123
    assert "re-engagement:123" in commitment.tags
    assert "touch:1" in commitment.tags
    assert commitment.text == "Checking in."


def test_does_not_duplicate_pending_touch(tmp_path: Path) -> None:
    _write_cfg(tmp_path)
    _write_template(tmp_path)
    _append_user(tmp_path, "2026-05-09T06:00:00+00:00")

    now = datetime(2026, 5, 11, 8, tzinfo=timezone.utc)
    assert run(tmp_path, now=now).queued
    second = run(tmp_path, now=now)
    assert second.queued == []
    assert second.skipped == ["123:already-pending"]


def test_active_chat_cancels_pending_reengagement(tmp_path: Path) -> None:
    _write_cfg(tmp_path)
    commitment = Commitment(
        slug="old-touch",
        created_at=parse_datetime("2026-05-11T01:00:00+00:00", field_name="created_at"),
        due_at=parse_datetime("2026-05-11T07:00:00+00:00", field_name="due_at"),
        action="telegram-send",
        chat_id=123,
        text="Checking in.",
        tags=("re-engagement", "re-engagement:123", "touch:1"),
        origin="reengage",
        metadata={"retries": 0},
    )
    add_commitment(tmp_path, commitment)
    _append_user(tmp_path, "2026-05-11T07:55:00+00:00")

    summary = run(tmp_path, now=datetime(2026, 5, 11, 8, tzinfo=timezone.utc))

    assert summary.canceled == ["old-touch"]
    assert not (tmp_path / "state" / "commitments" / "old-touch.yaml").exists()


def test_cancel_if_tracked_uses_config_gate(tmp_path: Path) -> None:
    _write_cfg(tmp_path)
    add_commitment(
        tmp_path,
        Commitment(
            slug="old-touch",
            created_at=parse_datetime("2026-05-11T01:00:00+00:00", field_name="created_at"),
            due_at=parse_datetime("2026-05-11T07:00:00+00:00", field_name="due_at"),
            action="telegram-send",
            chat_id=123,
            text="Checking in.",
            tags=("re-engagement", "re-engagement:123", "touch:1"),
            origin="reengage",
            metadata={"retries": 0},
        ),
    )
    assert cancel_if_tracked(tmp_path, 123) == ["old-touch"]


def test_transcript_fixture_is_jsonl(tmp_path: Path) -> None:
    _append_user(tmp_path, "2026-05-11T07:55:00+00:00")
    path = tmp_path / "state" / "transcripts" / "123.jsonl"
    assert json.loads(path.read_text().splitlines()[0])["role"] == "user"
