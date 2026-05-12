from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from commitments.actions import DispatchResult  # noqa: E402
from commitments.engine import add_commitment, cancel_by_tag, tick  # noqa: E402
from commitments.schema import Commitment, CommitmentError, load, parse_datetime  # noqa: E402


def _commitment(slug: str, due: str = "2026-05-11T09:00:00+00:00") -> Commitment:
    return Commitment(
        slug=slug,
        created_at=parse_datetime("2026-05-10T09:00:00+00:00", field_name="created_at"),
        due_at=parse_datetime(due, field_name="due_at"),
        action="telegram-send",
        chat_id=123,
        text="hello",
        tags=("follow-up", "deal-x"),
        origin="manual",
        metadata={"retries": 0},
    )


def test_schema_requires_explicit_timezone() -> None:
    with pytest.raises(CommitmentError):
        parse_datetime("2026-05-11T09:00:00", field_name="due_at")


def test_tick_fires_due_commitment_and_archives(tmp_path: Path) -> None:
    add_commitment(tmp_path, _commitment("deal-x"))

    def dispatcher(_instance: Path, commitment: Commitment) -> DispatchResult:
        assert commitment.slug == "deal-x"
        return DispatchResult(ok=True, message="sent")

    summary = tick(
        tmp_path,
        now=datetime(2026, 5, 11, 10, tzinfo=timezone.utc),
        dispatcher=dispatcher,
    )

    assert summary.ok
    assert summary.fired == ["deal-x"]
    assert not (tmp_path / "state" / "commitments" / "deal-x.yaml").exists()
    archived = list((tmp_path / "state" / "commitments" / "done").glob("deal-x.executed-*.yaml"))
    assert len(archived) == 1


def test_tick_retries_then_moves_to_failed(tmp_path: Path) -> None:
    add_commitment(tmp_path, _commitment("retry-me"))

    def dispatcher(_instance: Path, _commitment: Commitment) -> DispatchResult:
        return DispatchResult(ok=False, retryable=True, message="network")

    now = datetime(2026, 5, 11, 10, tzinfo=timezone.utc)
    assert not tick(tmp_path, now=now, dispatcher=dispatcher).ok
    first = load(tmp_path / "state" / "commitments" / "retry-me.yaml")
    assert first.metadata["retries"] == 1

    assert not tick(tmp_path, now=now, dispatcher=dispatcher).ok
    assert not tick(tmp_path, now=now, dispatcher=dispatcher).ok
    assert not (tmp_path / "state" / "commitments" / "retry-me.yaml").exists()
    failed = list((tmp_path / "state" / "commitments" / "failed").glob("retry-me.failed-*.yaml"))
    assert len(failed) == 1


def test_repeating_commitment_archives_copy_and_advances_due(tmp_path: Path) -> None:
    c = _commitment("daily").with_due_at(
        parse_datetime("2026-05-11T09:00:00+00:00", field_name="due_at")
    )
    c = Commitment(
        slug=c.slug,
        created_at=c.created_at,
        due_at=c.due_at,
        action=c.action,
        text=c.text,
        chat_id=c.chat_id,
        tags=c.tags,
        repeat="daily",
        origin=c.origin,
        metadata=c.metadata,
    )
    add_commitment(tmp_path, c)

    summary = tick(
        tmp_path,
        now=datetime(2026, 5, 11, 10, tzinfo=timezone.utc),
        dispatcher=lambda _i, _c: DispatchResult(ok=True),
    )

    assert summary.ok
    pending = load(tmp_path / "state" / "commitments" / "daily.yaml")
    assert pending.due_at.isoformat() == "2026-05-12T09:00:00+00:00"
    archived = list((tmp_path / "state" / "commitments" / "done").glob("daily.executed-*.yaml"))
    assert len(archived) == 1


def test_cancel_by_tag_removes_only_pending_matches(tmp_path: Path) -> None:
    add_commitment(tmp_path, _commitment("a"))
    add_commitment(tmp_path, _commitment("b"))
    canceled = cancel_by_tag(tmp_path, "deal-x")
    assert canceled == ["a", "b"]
    assert list((tmp_path / "state" / "commitments").glob("*.yaml")) == []
