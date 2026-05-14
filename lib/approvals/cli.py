"""CLI for the unified approvals table: list/show/approve/reject/expire/apply/gc."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .conf import load_approvals_config
from .models import (
    Approval,
    ApprovalConflict,
    ApprovalNotFound,
    SENSITIVE_KINDS,
    ApprovalStatus,
)


def _resolve_instance(arg: str | None) -> Path:
    try:
        from jc_paths import InstanceResolutionError, resolve_instance_dir

        return resolve_instance_dir(arg, fallback_markers=("memory", "state"))
    except Exception:
        candidate = Path(arg) if arg else Path(os.environ.get("JC_INSTANCE_DIR") or Path.cwd())
        return candidate.resolve()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    instance = _resolve_instance(getattr(args, "instance_dir", None))
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    return int(func(args, instance) or 0)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jc-approvals")
    p.add_argument("--instance-dir")
    sub = p.add_subparsers(dest="cmd")

    pl = sub.add_parser("list", help="List approvals")
    pl.add_argument("--status", default="pending")
    pl.add_argument("--kind")
    pl.add_argument("--limit", type=int, default=20)
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("show", help="Show one approval")
    ps.add_argument("approval_id")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_show)

    pa = sub.add_parser("approve", help="Approve a pending row")
    pa.add_argument("approval_id")
    pa.add_argument("--note")
    pa.set_defaults(func=cmd_approve)

    pr = sub.add_parser("reject", help="Reject a pending row")
    pr.add_argument("approval_id")
    pr.add_argument("--note")
    pr.set_defaults(func=cmd_reject)

    pe = sub.add_parser("expire", help="Force-expire a pending row")
    pe.add_argument("approval_id")
    pe.set_defaults(func=cmd_expire)

    papply = sub.add_parser(
        "apply", help="Rerun the apply callback for an already-approved row"
    )
    papply.add_argument("approval_id")
    papply.set_defaults(func=cmd_apply)

    pgc = sub.add_parser("gc", help="Hard-delete old terminal rows")
    pgc.add_argument("--days", type=int, default=None)
    pgc.add_argument("--dry-run", action="store_true")
    pgc.set_defaults(func=cmd_gc)

    return p


def cmd_list(args: argparse.Namespace, instance: Path) -> int:
    from .service import list_pending

    status = None if args.status == "all" else args.status
    records = list_pending(instance, status=status, kind=args.kind, limit=args.limit)
    if args.json:
        for rec in records:
            print(json.dumps(_record_to_dict(rec)))
        return 0

    if not records:
        print("(none)")
        return 0
    print(f"{'ID':10}{'KIND':22}{'STATUS':10}{'AGE':8}TITLE")
    for rec in records:
        age = _age_str(rec.requested_at)
        print(
            f"{rec.short_id:10}{rec.kind:22}{rec.status:10}{age:8}"
            f"{rec.title}"
        )
    return 0


def cmd_show(args: argparse.Namespace, instance: Path) -> int:
    from .service import get

    rec = _resolve_one(instance, args.approval_id)
    if rec is None:
        print(f"approval not found: {args.approval_id}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(_record_to_dict(rec), indent=2))
        return 0
    print(f"id:        {rec.approval_id}")
    print(f"kind:      {rec.kind}")
    print(f"title:     {rec.title}")
    print(f"status:    {rec.status}")
    print(f"producer:  {rec.producer}")
    if rec.source_ref:
        print(f"source:    {rec.source_ref}")
    print(f"requested: {rec.requested_at}")
    if rec.expires_at:
        print(f"expires:   {rec.expires_at}")
    if rec.decided_at:
        print(f"decided:   {rec.decided_at} via {rec.decision_channel} by {rec.decided_by}")
    if rec.applied_at:
        print(f"applied:   {rec.applied_at}")
    if rec.body:
        print()
        print(rec.body)
    if rec.payload:
        print()
        print("payload:")
        print(json.dumps(rec.payload, indent=2, sort_keys=True))
    if rec.result:
        print()
        print("result:")
        print(rec.result)
    return 0


def cmd_approve(args: argparse.Namespace, instance: Path) -> int:
    return _cmd_decide(args, instance, "approve")


def cmd_reject(args: argparse.Namespace, instance: Path) -> int:
    return _cmd_decide(args, instance, "reject")


def _cmd_decide(args: argparse.Namespace, instance: Path, action: str) -> int:
    from .service import decide, get

    cfg = load_approvals_config(instance)
    if cfg.cli_disabled:
        print("approvals CLI decide is disabled in ops/approvals.yaml", file=sys.stderr)
        return 9
    if cfg.cli_operator_uid is not None and os.getuid() != cfg.cli_operator_uid:
        print(
            f"approvals CLI decide refused: uid {os.getuid()} does not match "
            f"operator_uid {cfg.cli_operator_uid}",
            file=sys.stderr,
        )
        return 9

    rec = _resolve_one(instance, args.approval_id)
    if rec is None:
        print(f"approval not found: {args.approval_id}", file=sys.stderr)
        return 1
    if action == "approve" and rec.is_sensitive():
        print(
            "SENSITIVE approvals cannot be approved from the CLI — use Telegram "
            "or DKIM-signed email instead.",
            file=sys.stderr,
        )
        return 9
    try:
        updated = decide(
            instance,
            rec.approval_id,
            action=action,
            decided_by="cli",
            decision_channel="cli",
            note=args.note,
        )
    except ApprovalConflict as exc:
        print(f"conflict: {exc}", file=sys.stderr)
        return 9
    except ApprovalNotFound:
        print(f"approval not found: {args.approval_id}", file=sys.stderr)
        return 1
    print(f"{action}d: {updated.short_id} ({updated.kind})")
    if updated.result:
        print(updated.result)
    return 0


def cmd_expire(args: argparse.Namespace, instance: Path) -> int:
    from .service import expire

    rec = _resolve_one(instance, args.approval_id)
    if rec is None:
        print(f"approval not found: {args.approval_id}", file=sys.stderr)
        return 1
    updated = expire(instance, rec.approval_id)
    print(f"status: {updated.status}")
    return 0


def cmd_apply(args: argparse.Namespace, instance: Path) -> int:
    from .service import apply_callback_now

    rec = _resolve_one(instance, args.approval_id)
    if rec is None:
        print(f"approval not found: {args.approval_id}", file=sys.stderr)
        return 1
    if rec.status != ApprovalStatus.APPROVED.value:
        print(f"approval status is {rec.status}; apply only valid for approved", file=sys.stderr)
        return 9
    updated = apply_callback_now(instance, rec.approval_id)
    if updated and updated.result:
        print(updated.result)
    return 0


def cmd_gc(args: argparse.Namespace, instance: Path) -> int:
    from .db import connect

    cfg = load_approvals_config(instance)
    days = args.days if args.days is not None else cfg.retention_days
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    conn = connect(instance)
    try:
        rows = conn.execute(
            "SELECT approval_id, kind, status, decided_at FROM approvals "
            "WHERE status != 'pending' AND decided_at IS NOT NULL"
        ).fetchall()
        candidates: list[Any] = []
        for row in rows:
            try:
                ts = datetime.fromisoformat(row["decided_at"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if ts.timestamp() < cutoff:
                candidates.append(row)
        if args.dry_run:
            for row in candidates:
                print(f"would delete {row['approval_id'][:8]} {row['kind']} {row['status']}")
            print(f"total: {len(candidates)}")
            return 0
        for row in candidates:
            conn.execute(
                "DELETE FROM approvals WHERE approval_id = ?", (row["approval_id"],)
            )
        print(f"deleted: {len(candidates)}")
    finally:
        conn.close()
    return 0


def _resolve_one(instance: Path, approval_id: str) -> Approval | None:
    """Look up by full id or by short prefix."""
    from .db import connect, row_to_approval
    from .service import get

    rec = get(instance, approval_id)
    if rec is not None:
        return rec
    conn = connect(instance)
    try:
        rows = conn.execute(
            "SELECT * FROM approvals WHERE approval_id LIKE ? || '%' LIMIT 2",
            (approval_id,),
        ).fetchall()
        if len(rows) == 1:
            return row_to_approval(rows[0])
    finally:
        conn.close()
    return None


def _record_to_dict(rec: Approval) -> dict[str, Any]:
    return {
        "approval_id": rec.approval_id,
        "short_id": rec.short_id,
        "kind": rec.kind,
        "title": rec.title,
        "status": rec.status,
        "requested_at": rec.requested_at,
        "decided_at": rec.decided_at,
        "decided_by": rec.decided_by,
        "decision_channel": rec.decision_channel,
        "expires_at": rec.expires_at,
        "applied_at": rec.applied_at,
        "producer": rec.producer,
        "source_ref": rec.source_ref,
        "payload": rec.payload,
        "callback_kind": rec.callback_kind,
        "callback_payload": rec.callback_payload,
        "body": rec.body,
        "note": rec.note,
        "result": rec.result,
    }


def _age_str(requested_at: str) -> str:
    try:
        ts = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    delta = datetime.now(timezone.utc) - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d"


if __name__ == "__main__":
    sys.exit(main())
