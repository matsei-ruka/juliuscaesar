"""Service-level state machine + idempotency tests."""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

import pytest

from approvals import callbacks
from approvals.models import ApprovalConflict, ApprovalStatus
from approvals.service import (
    decide,
    expire,
    find_by_source,
    get,
    list_pending,
    raise_,
    wait,
)


def _action_payload() -> dict:
    return {"description": "test"}


def test_raise_creates_pending(instance_dir: Path) -> None:
    rec = raise_(
        instance_dir,
        kind="action",
        title="do x",
        payload=_action_payload(),
        producer="test",
        notify_telegram=False,
    )
    assert rec.status == ApprovalStatus.PENDING.value
    assert len(rec.approval_id) == 32
    assert len(rec.callback_token) == 64
    assert rec.callback_kind == "action"


def test_raise_is_idempotent_on_source_ref(instance_dir: Path) -> None:
    rec1 = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload=_action_payload(),
        producer="test",
        source_ref="src:1",
        notify_telegram=False,
    )
    rec2 = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload=_action_payload(),
        producer="test",
        source_ref="src:1",
        notify_telegram=False,
    )
    assert rec1.approval_id == rec2.approval_id


def test_decide_approve(instance_dir: Path) -> None:
    rec = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload=_action_payload(),
        producer="test",
        notify_telegram=False,
    )
    updated = decide(
        instance_dir,
        rec.approval_id,
        action="approve",
        decided_by="cli",
        decision_channel="cli",
    )
    assert updated.status == ApprovalStatus.APPROVED.value
    assert updated.decided_by == "cli"
    assert updated.applied_at is not None  # passthrough callback set it


def test_decide_reject(instance_dir: Path) -> None:
    rec = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload=_action_payload(),
        producer="test",
        notify_telegram=False,
    )
    updated = decide(
        instance_dir,
        rec.approval_id,
        action="reject",
        decided_by="cli",
        decision_channel="cli",
    )
    assert updated.status == ApprovalStatus.REJECTED.value


def test_decide_idempotent_same_action(instance_dir: Path) -> None:
    rec = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload=_action_payload(),
        producer="test",
        notify_telegram=False,
    )
    first = decide(
        instance_dir,
        rec.approval_id,
        action="approve",
        decided_by="cli",
        decision_channel="cli",
    )
    second = decide(
        instance_dir,
        rec.approval_id,
        action="approve",
        decided_by="cli",
        decision_channel="cli",
    )
    assert first.approval_id == second.approval_id
    assert second.status == ApprovalStatus.APPROVED.value


def test_decide_conflict_raises(instance_dir: Path) -> None:
    rec = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload=_action_payload(),
        producer="test",
        notify_telegram=False,
    )
    decide(
        instance_dir,
        rec.approval_id,
        action="approve",
        decided_by="cli",
        decision_channel="cli",
    )
    with pytest.raises(ApprovalConflict):
        decide(
            instance_dir,
            rec.approval_id,
            action="reject",
            decided_by="cli",
            decision_channel="cli",
        )


def test_expire_via_overridden_clock(instance_dir: Path) -> None:
    rec = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload=_action_payload(),
        producer="test",
        notify_telegram=False,
        expires_in=timedelta(seconds=1),
    )
    time.sleep(1.1)
    fetched = get(instance_dir, rec.approval_id)
    assert fetched is not None
    assert fetched.status == ApprovalStatus.EXPIRED.value


def test_list_pending_filters(instance_dir: Path) -> None:
    r1 = raise_(
        instance_dir,
        kind="action",
        title="a",
        payload=_action_payload(),
        producer="test",
        notify_telegram=False,
    )
    raise_(
        instance_dir,
        kind="message",
        title="m",
        payload={"channel": "tg", "recipient": "x", "body_preview": "p"},
        producer="test",
        notify_telegram=False,
    )
    rows = list_pending(instance_dir, kind="action")
    assert len(rows) == 1
    assert rows[0].approval_id == r1.approval_id


def test_callback_token_mismatch_rejected(instance_dir: Path) -> None:
    rec = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload=_action_payload(),
        producer="test",
        notify_telegram=False,
    )
    with pytest.raises(PermissionError):
        decide(
            instance_dir,
            rec.approval_id,
            action="approve",
            decided_by="email:x@y.com",
            decision_channel="email",
            callback_token="0" * 64,
        )


def test_find_by_source(instance_dir: Path) -> None:
    rec = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload=_action_payload(),
        producer="prod",
        source_ref="ref:42",
        notify_telegram=False,
    )
    looked = find_by_source(instance_dir, "prod", "ref:42")
    assert looked is not None and looked.approval_id == rec.approval_id


def test_expire_admin_override(instance_dir: Path) -> None:
    rec = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload=_action_payload(),
        producer="test",
        notify_telegram=False,
    )
    updated = expire(instance_dir, rec.approval_id)
    assert updated.status == ApprovalStatus.EXPIRED.value


def test_callback_runs_on_approve(instance_dir: Path) -> None:
    seen: dict[str, str] = {}

    def handler(_instance, record):
        seen["id"] = record.approval_id
        return {"echoed": record.title}

    callbacks.register("custom_kind", handler)
    try:
        rec = raise_(
            instance_dir,
            kind="action",
            title="t",
            payload=_action_payload(),
            callback_kind="custom_kind",
            producer="test",
            notify_telegram=False,
        )
        updated = decide(
            instance_dir,
            rec.approval_id,
            action="approve",
            decided_by="cli",
            decision_channel="cli",
        )
        assert seen["id"] == rec.approval_id
        assert updated.result and "echoed" in updated.result
    finally:
        callbacks.unregister("custom_kind")


def test_wait_returns_when_terminal(instance_dir: Path) -> None:
    rec = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload=_action_payload(),
        producer="test",
        notify_telegram=False,
    )
    decide(
        instance_dir,
        rec.approval_id,
        action="reject",
        decided_by="cli",
        decision_channel="cli",
    )
    waited = wait(instance_dir, rec.approval_id, timeout=timedelta(seconds=1))
    assert waited.status == ApprovalStatus.REJECTED.value


def test_wait_times_out(instance_dir: Path) -> None:
    rec = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload=_action_payload(),
        producer="test",
        notify_telegram=False,
    )
    started = time.monotonic()
    waited = wait(
        instance_dir,
        rec.approval_id,
        timeout=timedelta(milliseconds=200),
        poll_chunk=timedelta(milliseconds=50),
    )
    elapsed = time.monotonic() - started
    assert waited.status == ApprovalStatus.PENDING.value
    assert elapsed >= 0.15
