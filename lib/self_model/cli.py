"""CLI for autonomous self_model updates."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous self_model updates")
    parser.add_argument("--instance-dir", type=Path, default=Path.cwd(), help="Instance directory")
    subparsers = parser.add_subparsers(dest="command", help="Subcommand")

    subparsers.add_parser("run", help="Execute one cycle")
    subparsers.add_parser("status", help="Show pending proposals + last run")

    list_parser = subparsers.add_parser("list", help="List proposals (default: staging)")
    list_parser.add_argument("--state", default="staging",
                             choices=["staging", "applied", "rejected", "dry-run"])
    list_parser.add_argument("--id", help="Filter by proposal ID")
    list_parser.add_argument("--limit", type=int, default=10, help="Max proposals to show")

    approve_parser = subparsers.add_parser(
        "approve",
        help="Apply a proposal (requires DKIM email approval for RULES/IDENTITY)",
    )
    approve_parser.add_argument("proposal_id", help="Proposal ID")

    reject_parser = subparsers.add_parser("reject", help="Reject a proposal")
    reject_parser.add_argument("proposal_id", help="Proposal ID")
    reject_parser.add_argument("--reason", help="Reason for rejection")
    reject_parser.add_argument(
        "--ignore-as-signal",
        action="store_true",
        help="Mark this proposal type as 'ignore as signal' — pattern will not be re-flagged",
    )

    history_parser = subparsers.add_parser("history", help="Show applied + rejected lists")
    history_parser.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()

    if args.command == "run":
        return cmd_run(args.instance_dir)
    elif args.command == "status":
        return cmd_status(args.instance_dir)
    elif args.command == "list":
        return cmd_list(args.instance_dir, args.state, args.id, args.limit)
    elif args.command == "approve":
        return cmd_approve(args.instance_dir, args.proposal_id)
    elif args.command == "reject":
        return cmd_reject(args.instance_dir, args.proposal_id,
                          args.reason or "", args.ignore_as_signal)
    elif args.command == "history":
        return cmd_history(args.instance_dir, args.limit)
    else:
        parser.print_help()
        return 1


def cmd_run(instance_dir: Path) -> int:
    """Execute one cycle."""
    from .runner import run_now
    return run_now(instance_dir)


def cmd_status(instance_dir: Path) -> int:
    """Show counts across states."""
    from .store import count_proposals
    print(f"Pending (staging):  {count_proposals(instance_dir, 'staging')}")
    print(f"Applied:            {count_proposals(instance_dir, 'applied')}")
    print(f"Rejected:           {count_proposals(instance_dir, 'rejected')}")
    print(f"Dry-run (logged):   {count_proposals(instance_dir, 'dry-run')}")
    return 0


def cmd_list(
    instance_dir: Path,
    state: str = "staging",
    proposal_id: str | None = None,
    limit: int = 10,
) -> int:
    """List proposals in given state."""
    from .store import load_proposals
    count = 0
    for proposal in load_proposals(instance_dir, state):
        if proposal_id and proposal.id != proposal_id:
            continue
        target = proposal.target_section or proposal.target_file
        print(f"[{proposal.id}] {proposal.type} {target}")
        print(f"  Confidence: {proposal.confidence:.2f}")
        print(f"  Reasoning:  {proposal.reasoning}")
        if proposal.supporting_evidence:
            print(f"  Evidence:   {', '.join(proposal.supporting_evidence)}")
        print()
        count += 1
        if count >= limit:
            break
    if count == 0:
        print(f"No proposals in state '{state}'.")
    return 0


def cmd_approve(instance_dir: Path, proposal_id: str) -> int:
    """Apply a proposal. The applier enforces DKIM gate for RULES/IDENTITY."""
    from .applier import apply_proposal, ApplierError
    from .store import load_proposals, move_proposal

    for proposal in load_proposals(instance_dir, "staging"):
        if proposal.id == proposal_id:
            try:
                apply_proposal(instance_dir, proposal)
                move_proposal(instance_dir, proposal_id, "staging", "applied")
                print(f"Applied {proposal_id} -> {proposal.target_file}")
                return 0
            except ApplierError as e:
                print(f"Error: {e}", file=sys.stderr)
                print(
                    "Hint: RULES.md / IDENTITY.md changes require a DKIM-signed approval "
                    "email from filippo.perta@scovai.com referencing the proposal id.",
                    file=sys.stderr,
                )
                return 1
    print(f"Proposal not found in staging: {proposal_id}", file=sys.stderr)
    return 1


def cmd_reject(
    instance_dir: Path,
    proposal_id: str,
    reason: str = "",
    ignore_as_signal: bool = False,
) -> int:
    """Reject a proposal. Optionally record as 'ignore-as-signal' in rejected-proposals/."""
    from .store import move_proposal, load_proposals

    target_proposal = None
    for proposal in load_proposals(instance_dir, "staging"):
        if proposal.id == proposal_id:
            target_proposal = proposal
            break

    if target_proposal is None:
        print(f"Proposal not found in staging: {proposal_id}", file=sys.stderr)
        return 1

    move_proposal(instance_dir, proposal_id, "staging", "rejected")

    # Persist rejection reason in memory/L2/rejected-proposals/<id>.md so the proposer
    # can later read this directory to learn "ignore-as-signal" patterns.
    rejected_dir = instance_dir / "memory" / "L2" / "rejected-proposals"
    rejected_dir.mkdir(parents=True, exist_ok=True)
    rejected_file = rejected_dir / f"{proposal_id}.md"
    body = f"""---
slug: rejected-{proposal_id}
title: Rejected proposal {proposal_id}
layer: L2
type: rejected-proposal
created: {target_proposal.created_at}
target_file: {target_proposal.target_file}
target_section: {target_proposal.target_section or 'TOP'}
ignore_as_signal: {str(ignore_as_signal).lower()}
---

## Reason
{reason or '(none provided)'}

## Original reasoning
{target_proposal.reasoning}

## Supporting evidence
{', '.join(target_proposal.supporting_evidence) if target_proposal.supporting_evidence else '(none)'}
"""
    rejected_file.write_text(body, encoding="utf-8")

    print(f"Rejected {proposal_id}; logged to {rejected_file}")
    if ignore_as_signal:
        print("Marked ignore-as-signal — proposer will skip this pattern in future cycles.")
    return 0


def cmd_history(instance_dir: Path, limit: int = 20) -> int:
    """Show applied + rejected proposals."""
    from .store import load_proposals
    print("=== Applied ===")
    count = 0
    for proposal in load_proposals(instance_dir, "applied"):
        print(f"[{proposal.id}] {proposal.type} {proposal.target_section or proposal.target_file}")
        count += 1
        if count >= limit:
            break
    if count == 0:
        print("(none)")

    print()
    print("=== Rejected ===")
    count = 0
    for proposal in load_proposals(instance_dir, "rejected"):
        print(f"[{proposal.id}] {proposal.type} {proposal.target_section or proposal.target_file}")
        count += 1
        if count >= limit:
            break
    if count == 0:
        print("(none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
