"""CLI for autonomous user model updates."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .conf import load_config
from .runner import run_now


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous user model updates")
    parser.add_argument("--instance-dir", type=Path, default=Path.cwd(), help="Instance directory")
    subparsers = parser.add_subparsers(dest="command", help="Subcommand")

    # run-now
    subparsers.add_parser("run-now", help="Execute one cycle")

    # status
    subparsers.add_parser("status", help="Show pending proposals + last run")

    # review
    review_parser = subparsers.add_parser("review", help="List pending proposals")
    review_parser.add_argument("--id", help="Filter by proposal ID")
    review_parser.add_argument("--limit", type=int, default=10, help="Max proposals to show")

    # apply
    apply_parser = subparsers.add_parser("apply", help="Apply a proposal")
    apply_parser.add_argument("proposal_id", help="Proposal ID")

    # reject
    reject_parser = subparsers.add_parser("reject", help="Reject a proposal")
    reject_parser.add_argument("proposal_id", help="Proposal ID")
    reject_parser.add_argument("--reason", help="Reason for rejection")

    # install
    install_parser = subparsers.add_parser("install", help="Install cron task")
    install_parser.add_argument("--cadence", default="0 3 * * *", help="Cron expression")

    # uninstall
    subparsers.add_parser("uninstall", help="Remove cron task")

    args = parser.parse_args()

    if args.command == "run-now":
        return run_now(args.instance_dir)
    elif args.command == "status":
        return cmd_status(args.instance_dir)
    elif args.command == "review":
        return cmd_review(args.instance_dir, args.id, args.limit)
    elif args.command == "apply":
        return cmd_apply(args.instance_dir, args.proposal_id)
    elif args.command == "reject":
        return cmd_reject(args.instance_dir, args.proposal_id, args.reason or "")
    elif args.command == "install":
        return cmd_install(args.instance_dir, args.cadence)
    elif args.command == "uninstall":
        return cmd_uninstall(args.instance_dir)
    else:
        parser.print_help()
        return 1


def cmd_status(instance_dir: Path) -> int:
    """Show pending proposal count + last run time."""
    from .store import count_proposals
    pending = count_proposals(instance_dir, "staging")
    print(f"Pending proposals: {pending}")
    # TODO: read last run time from state file
    return 0


def cmd_review(instance_dir: Path, proposal_id: str | None = None, limit: int = 10) -> int:
    """List pending proposals."""
    from .store import load_proposals
    count = 0
    for proposal in load_proposals(instance_dir, "staging"):
        if proposal_id and proposal.id != proposal_id:
            continue
        print(f"[{proposal.id}] {proposal.type} {proposal.target_section or proposal.target_file}")
        print(f"  Confidence: {proposal.confidence:.2f}")
        print(f"  Reasoning: {proposal.reasoning}")
        print()
        count += 1
        if count >= limit:
            break
    return 0


def cmd_apply(instance_dir: Path, proposal_id: str) -> int:
    """Apply a proposal."""
    from .applier import apply_proposal, ApplierError
    from .store import load_proposals, move_proposal
    for proposal in load_proposals(instance_dir, "staging"):
        if proposal.id == proposal_id:
            try:
                apply_proposal(instance_dir, proposal)
                move_proposal(instance_dir, proposal_id, "staging", "applied")
                print(f"Applied {proposal_id}")
                return 0
            except ApplierError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
    print(f"Proposal not found: {proposal_id}", file=sys.stderr)
    return 1


def cmd_reject(instance_dir: Path, proposal_id: str, reason: str = "") -> int:
    """Reject a proposal."""
    from .store import move_proposal
    move_proposal(instance_dir, proposal_id, "staging", "rejected")
    print(f"Rejected {proposal_id}")
    return 0


def cmd_install(instance_dir: Path, cadence: str) -> int:
    """Install cron task."""
    import subprocess
    binary = "jc-user-model"
    cron_line = f"{cadence} {binary} run-now --instance-dir {instance_dir}  # jc-user-model for {instance_dir}"
    proc = subprocess.run(
        ["bash", "-c", f"(crontab -l 2>/dev/null || true) | grep -v '{binary}' | crontab - && crontab -l | grep -q '{binary}' || (crontab -l 2>/dev/null; echo '{cron_line}') | crontab -"],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        print(f"Cron task installed")
        return 0
    print(f"Failed to install cron: {proc.stderr}", file=sys.stderr)
    return 1


def cmd_uninstall(instance_dir: Path) -> int:
    """Remove cron task."""
    import subprocess
    binary = "jc-user-model"
    proc = subprocess.run(
        ["bash", "-c", f"(crontab -l 2>/dev/null || true) | grep -v '{binary}' | crontab -"],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        print(f"Cron task removed")
        return 0
    print(f"Failed to uninstall cron: {proc.stderr}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
