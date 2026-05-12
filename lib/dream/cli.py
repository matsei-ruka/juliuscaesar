"""CLI for jc-dream."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from jc_paths import InstanceResolutionError, resolve_instance_dir as _resolve_instance_dir

from .apply import approve, pending, reject
from .runner import run_dream


def resolve_instance_dir(arg: str | None) -> Path:
    try:
        return _resolve_instance_dir(arg, fallback_markers=("memory", "state"))
    except InstanceResolutionError as exc:
        raise SystemExit(str(exc)) from exc


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)


def cmd_tick(args: argparse.Namespace, instance: Path) -> int:
    result = run_dream(instance, dry_run=args.dry_run)
    _print_result(result)
    return 0


def cmd_run(args: argparse.Namespace, instance: Path) -> int:
    result = run_dream(
        instance,
        since=parse_ts(args.since),
        until=parse_ts(args.until),
        dry_run=args.dry_run,
    )
    _print_result(result)
    return 0


def cmd_list(_args: argparse.Namespace, instance: Path) -> int:
    root = instance / "state" / "dreams"
    reports = sorted(root.glob("*.md"), reverse=True) if root.exists() else []
    if not reports:
        print("(no dreams)")
        return 0
    for path in reports:
        print(path.name)
    return 0


def cmd_show(args: argparse.Namespace, instance: Path) -> int:
    path = instance / "state" / "dreams" / f"{args.dream_id.removesuffix('.md')}.md"
    if not path.exists():
        print(f"dream not found: {args.dream_id}", file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8"), end="")
    return 0


def cmd_pending(_args: argparse.Namespace, instance: Path) -> int:
    items = pending(instance)
    if not items:
        print("(no pending dream diffs)")
        return 0
    for path in items:
        print(path.stem)
    return 0


def cmd_approve(args: argparse.Namespace, instance: Path) -> int:
    path = approve(instance, args.diff_id)
    print(path)
    return 0


def cmd_reject(args: argparse.Namespace, instance: Path) -> int:
    print(reject(instance, args.diff_id))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jc-dream")
    p.add_argument("--instance-dir")
    sub = p.add_subparsers(dest="cmd", required=True)

    pt = sub.add_parser("tick", help="Run one dream cycle")
    pt.add_argument("--dry-run", action="store_true")
    pt.set_defaults(func=cmd_tick)

    pd = sub.add_parser("dry-run", help="Run phases without writing artifacts or report")
    pd.set_defaults(func=lambda args, instance: cmd_tick(argparse.Namespace(dry_run=True), instance))

    pr = sub.add_parser("run", help="Replay over a window")
    pr.add_argument("--since")
    pr.add_argument("--until")
    pr.add_argument("--dry-run", action="store_true")
    pr.set_defaults(func=cmd_run)

    pl = sub.add_parser("list", help="List dream reports")
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("show", help="Show one dream report")
    ps.add_argument("dream_id")
    ps.set_defaults(func=cmd_show)

    pp = sub.add_parser("pending", help="List staged sensitive diffs")
    pp.set_defaults(func=cmd_pending)

    pa = sub.add_parser("approve", help="Apply a staged sensitive diff")
    pa.add_argument("diff_id")
    pa.set_defaults(func=cmd_approve)

    pj = sub.add_parser("reject", help="Reject staged diff or roll back retained auto diff")
    pj.add_argument("diff_id")
    pj.set_defaults(func=cmd_reject)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    instance = resolve_instance_dir(args.instance_dir)
    return args.func(args, instance)


def _print_result(result) -> None:
    print(f"dream_id={result.dream_id}")
    print(f"status={result.status}")
    print(f"artifacts={len(result.artifacts)}")
    if result.report_path:
        print(f"report={result.report_path}")
