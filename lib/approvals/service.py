"""Public approval service: raise_, wait, decide, get, list_pending, expire."""

from __future__ import annotations

import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import callbacks
from .conf import (
    ApprovalsConfig,
    kind_expires_hours,
    kind_notify_email_default,
    load_approvals_config,
)
from .db import connect, immediate_tx, row_to_approval, utc_now_iso
from .models import (
    Approval,
    ApprovalConflict,
    ApprovalNotFound,
    ApprovalStatus,
)
from .principal import load_principal
from .schema import validate_kind, validate_payload


logger = logging.getLogger("approvals.service")


def raise_(
    instance_dir: Path,
    *,
    kind: str,
    title: str,
    payload: dict[str, Any],
    producer: str,
    body: str = "",
    callback_kind: str | None = None,
    callback_payload: dict[str, Any] | None = None,
    source_ref: str | None = None,
    expires_in: timedelta | None = None,
    notify_telegram: bool = True,
    notify_email: bool | None = None,
    media_paths: tuple[str, ...] = (),
) -> Approval:
    """Create a new pending approval (idempotent on `(producer, source_ref)`)."""
    kind = validate_kind(kind)
    payload = validate_payload(kind, dict(payload or {}))
    if not title:
        raise ValueError("title is required")
    if not producer:
        raise ValueError("producer is required")

    instance_dir = Path(instance_dir)
    cfg = load_approvals_config(instance_dir)
    callback_kind = callback_kind or kind
    callback_payload = dict(callback_payload or {})

    conn = connect(instance_dir)
    try:
        if source_ref:
            existing = _select_by_source(conn, producer, source_ref)
            if existing is not None:
                _maybe_notify(instance_dir, existing)
                return existing

        approval_id = uuid.uuid4().hex
        callback_token = secrets.token_hex(32)
        requested_at = utc_now_iso()
        if expires_in is None:
            hours = kind_expires_hours(cfg, kind)
            expires_in = timedelta(hours=hours) if hours > 0 else None
        expires_at = _format_iso(_now_utc() + expires_in) if expires_in else None
        notify_email_flag = (
            kind_notify_email_default(cfg, kind) if notify_email is None else bool(notify_email)
        )

        principal = load_principal(instance_dir)
        if notify_telegram and not principal.telegram_chat_id:
            if cfg.require_telegram:
                raise RuntimeError(
                    "approvals: main chat unresolved — refusing to enqueue "
                    "(set principal.telegram_chat_id in ops/gateway.yaml or "
                    "TELEGRAM_CHAT_ID in .env)"
                )
            logger.warning(
                "approvals: main chat unresolved — telegram notify disabled for "
                "this row (kind=%s, source_ref=%s)",
                kind,
                source_ref,
            )
            notify_telegram = False
            if not notify_email_flag:
                notify_email_flag = bool(principal.email)

        conn.execute(
            """
            INSERT INTO approvals (
                approval_id, kind, title, body, payload, status, requested_at,
                expires_at, callback_token, callback_kind, callback_payload,
                producer, source_ref, notify_telegram, notify_email, media_paths
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id,
                kind,
                title,
                body or "",
                json.dumps(payload, sort_keys=True),
                ApprovalStatus.PENDING.value,
                requested_at,
                expires_at,
                callback_token,
                callback_kind,
                json.dumps(callback_payload, sort_keys=True),
                producer,
                source_ref,
                1 if notify_telegram else 0,
                1 if notify_email_flag else 0,
                json.dumps(list(media_paths)),
            ),
        )
        record = _select_by_id(conn, approval_id)
        assert record is not None
        _maybe_notify(instance_dir, record)
        return record
    finally:
        conn.close()


def wait(
    instance_dir: Path,
    approval_id: str,
    *,
    timeout: timedelta = timedelta(minutes=10),
    poll_chunk: timedelta = timedelta(seconds=2),
) -> Approval:
    """Block until the approval reaches a terminal state or `timeout` elapses."""
    deadline = time.monotonic() + max(0.0, timeout.total_seconds())
    poll = max(0.05, poll_chunk.total_seconds())
    while True:
        record = get(instance_dir, approval_id)
        if record is None:
            raise ApprovalNotFound(approval_id)
        if record.is_terminal():
            return record
        if time.monotonic() >= deadline:
            return record
        time.sleep(poll)


def decide(
    instance_dir: Path,
    approval_id: str,
    *,
    action: str,
    decided_by: str,
    decision_channel: str,
    callback_token: str | None = None,
    note: str | None = None,
    run_callback: bool = True,
) -> Approval:
    """Flip a pending row terminal (idempotent on `(approval_id, action)`)."""
    if action not in ("approve", "reject"):
        raise ValueError(f"action must be approve/reject, got {action!r}")
    instance_dir = Path(instance_dir)

    conn = connect(instance_dir)
    try:
        with immediate_tx(conn):
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise ApprovalNotFound(approval_id)
            record = row_to_approval(row)
            assert record is not None

            target_status = (
                ApprovalStatus.APPROVED.value
                if action == "approve"
                else ApprovalStatus.REJECTED.value
            )

            if record.status != ApprovalStatus.PENDING.value:
                if record.status == target_status:
                    return record
                raise ApprovalConflict(
                    f"approval {approval_id} is {record.status}; cannot {action}"
                )

            if record.expires_at and _is_expired(record.expires_at):
                conn.execute(
                    "UPDATE approvals SET status = ?, decided_at = ?, "
                    "decided_by = ?, decision_channel = ? WHERE approval_id = ?",
                    (
                        ApprovalStatus.EXPIRED.value,
                        utc_now_iso(),
                        "system:expire",
                        "system",
                        approval_id,
                    ),
                )
                refreshed = _select_by_id(conn, approval_id)
                assert refreshed is not None
                raise ApprovalConflict(
                    f"approval {approval_id} expired at {record.expires_at}"
                )

            if callback_token is not None and callback_token != record.callback_token:
                raise PermissionError("callback_token mismatch")

            now = utc_now_iso()
            conn.execute(
                """
                UPDATE approvals
                   SET status = ?, decided_at = ?, decided_by = ?,
                       decision_channel = ?, note = COALESCE(?, note)
                 WHERE approval_id = ? AND status = ?
                """,
                (
                    target_status,
                    now,
                    decided_by,
                    decision_channel,
                    note,
                    approval_id,
                    ApprovalStatus.PENDING.value,
                ),
            )
            updated_row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            updated = row_to_approval(updated_row)
            assert updated is not None

        if action == "approve" and run_callback:
            _run_callback(instance_dir, updated)
            return get(instance_dir, approval_id) or updated
        return updated
    finally:
        conn.close()


def expire(instance_dir: Path, approval_id: str) -> Approval:
    """Operator override: mark a pending row expired."""
    conn = connect(Path(instance_dir))
    try:
        with immediate_tx(conn):
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise ApprovalNotFound(approval_id)
            record = row_to_approval(row)
            assert record is not None
            if record.status != ApprovalStatus.PENDING.value:
                return record
            conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, "
                "decision_channel = ? WHERE approval_id = ?",
                (
                    ApprovalStatus.EXPIRED.value,
                    utc_now_iso(),
                    "cli:expire",
                    "cli",
                    approval_id,
                ),
            )
            updated_row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            updated = row_to_approval(updated_row)
        assert updated is not None
        return updated
    finally:
        conn.close()


def get(instance_dir: Path, approval_id: str) -> Approval | None:
    """Return one approval or None — applies just-in-time expiry pass."""
    conn = connect(Path(instance_dir))
    try:
        record = _select_by_id(conn, approval_id)
        if record is None:
            return None
        if (
            record.status == ApprovalStatus.PENDING.value
            and record.expires_at
            and _is_expired(record.expires_at)
        ):
            conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, "
                "decision_channel = ? WHERE approval_id = ?",
                (
                    ApprovalStatus.EXPIRED.value,
                    utc_now_iso(),
                    "system:expire",
                    "system",
                    approval_id,
                ),
            )
            record = _select_by_id(conn, approval_id)
        return record
    finally:
        conn.close()


def list_pending(
    instance_dir: Path,
    *,
    status: str | None = "pending",
    kind: str | None = None,
    limit: int = 100,
) -> list[Approval]:
    """List approvals filtered by status/kind (newest first)."""
    conn = connect(Path(instance_dir))
    try:
        clauses: list[str] = []
        args: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            args.append(status)
        if kind is not None:
            clauses.append("kind = ?")
            args.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM approvals{where} ORDER BY requested_at DESC LIMIT ?",
            (*args, int(limit)),
        ).fetchall()
        records = [row_to_approval(r) for r in rows]
        if status == ApprovalStatus.PENDING.value:
            expired_ids: list[str] = []
            for rec in records:
                if rec and rec.expires_at and _is_expired(rec.expires_at):
                    expired_ids.append(rec.approval_id)
            for aid in expired_ids:
                conn.execute(
                    "UPDATE approvals SET status = ?, decided_at = ?, "
                    "decided_by = ?, decision_channel = ? WHERE approval_id = ?",
                    (
                        ApprovalStatus.EXPIRED.value,
                        utc_now_iso(),
                        "system:expire",
                        "system",
                        aid,
                    ),
                )
            if expired_ids:
                records = [r for r in records if r and r.approval_id not in expired_ids]
        return [r for r in records if r is not None]
    finally:
        conn.close()


def find_by_source(
    instance_dir: Path, producer: str, source_ref: str
) -> Approval | None:
    """Look up the unique pending/decided row for a producer's record."""
    conn = connect(Path(instance_dir))
    try:
        return _select_by_source(conn, producer, source_ref)
    finally:
        conn.close()


def mark_notified(
    instance_dir: Path,
    approval_id: str,
    *,
    extra_callback_payload: dict[str, Any] | None = None,
) -> None:
    """Stamp `notified_at`; optionally merge `extra_callback_payload`."""
    conn = connect(Path(instance_dir))
    try:
        record = _select_by_id(conn, approval_id)
        if record is None:
            return
        merged = dict(record.callback_payload or {})
        if extra_callback_payload:
            merged.update(extra_callback_payload)
        conn.execute(
            "UPDATE approvals SET notified_at = COALESCE(notified_at, ?), "
            "callback_payload = ? WHERE approval_id = ?",
            (utc_now_iso(), json.dumps(merged, sort_keys=True), approval_id),
        )
    finally:
        conn.close()


def _maybe_notify(instance_dir: Path, record: Approval) -> None:
    """Fan out to telegram/email notifiers. Failures logged, never raised."""
    if record.is_terminal():
        return
    try:
        from .channels import telegram as telegram_channel

        telegram_channel.notify(instance_dir, record)
    except Exception as exc:  # noqa: BLE001
        logger.warning("approvals telegram notify failed id=%s: %s", record.approval_id, exc)
    if record.notify_email:
        try:
            from .channels import email as email_channel

            email_channel.notify(instance_dir, record)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "approvals email notify failed id=%s: %s", record.approval_id, exc
            )


def _run_callback(instance_dir: Path, record: Approval) -> None:
    """Run the registered apply callback for `record.callback_kind`."""
    callbacks.ensure_defaults_loaded()
    handler = callbacks.get(record.callback_kind)
    if handler is None:
        logger.info(
            "approvals: no callback registered for kind=%s (id=%s)",
            record.callback_kind,
            record.approval_id,
        )
        return
    conn = connect(instance_dir)
    try:
        try:
            result = handler(instance_dir, record) or {}
            payload = {"ok": True, "result": result}
            conn.execute(
                "UPDATE approvals SET applied_at = ?, result = ? WHERE approval_id = ?",
                (utc_now_iso(), json.dumps(payload, sort_keys=True), record.approval_id),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "approvals callback failed id=%s kind=%s",
                record.approval_id,
                record.callback_kind,
            )
            payload = {"ok": False, "error": str(exc), "error_class": exc.__class__.__name__}
            conn.execute(
                "UPDATE approvals SET result = ? WHERE approval_id = ?",
                (json.dumps(payload, sort_keys=True), record.approval_id),
            )
    finally:
        conn.close()


def apply_callback_now(instance_dir: Path, approval_id: str) -> Approval | None:
    """Re-run the apply callback for an already-approved row (operator-driven)."""
    record = get(instance_dir, approval_id)
    if record is None:
        return None
    if record.status != ApprovalStatus.APPROVED.value:
        return record
    _run_callback(instance_dir, record)
    return get(instance_dir, approval_id)


def _select_by_id(conn, approval_id: str) -> Approval | None:
    row = conn.execute(
        "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
    ).fetchone()
    return row_to_approval(row)


def _select_by_source(conn, producer: str, source_ref: str) -> Approval | None:
    row = conn.execute(
        "SELECT * FROM approvals WHERE producer = ? AND source_ref = ? "
        "ORDER BY requested_at DESC LIMIT 1",
        (producer, source_ref),
    ).fetchone()
    return row_to_approval(row)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _format_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_expired(expires_at: str) -> bool:
    try:
        text = expires_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return False
    return _now_utc() >= dt
