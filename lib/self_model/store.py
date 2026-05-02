"""JSONL storage for proposals, applied, and rejected entries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class Proposal:
    """A self model update proposal."""
    id: str
    created_at: str
    type: str  # add | modify | remove
    target_file: str
    target_section: str | None
    current_content: str
    proposed_content: str
    reasoning: str
    confidence: float
    supporting_evidence: list[str]
    content_hash: str


def content_hash(target_file: str, target_section: str | None, proposed_content: str) -> str:
    """Generate sha256 hash for dedup."""
    data = f"{target_file}|{target_section}|{proposed_content}"
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()[:12]


def load_proposals(instance_dir: Path, state: str = "staging") -> Iterator[Proposal]:
    """Load proposals from storage. State: staging | applied | rejected | dry-run."""
    path = instance_dir / "memory" / "staging" / f"self-model-{state}.jsonl"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            yield Proposal(**data)
        except (json.JSONDecodeError, TypeError):
            continue


def save_proposal(instance_dir: Path, proposal: Proposal, state: str = "staging") -> None:
    """Append proposal to JSONL file."""
    path = instance_dir / "memory" / "staging" / f"self-model-{state}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(proposal)) + "\n")


def has_recent_proposal(
    instance_dir: Path,
    content_hash_val: str,
    cooldown_days: int = 30,
) -> bool:
    """Check if proposal with same hash was made within cooldown period."""
    path = instance_dir / "memory" / "staging" / "self-model-staging.jsonl"
    if not path.exists():
        return False

    cutoff = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()
    for proposal in load_proposals(instance_dir, "staging"):
        if proposal.content_hash == content_hash_val and proposal.created_at >= cutoff:
            return True
    return False


def move_proposal(
    instance_dir: Path,
    proposal_id: str,
    from_state: str = "staging",
    to_state: str = "applied",
) -> None:
    """Move proposal between states (staging → applied/rejected)."""
    from_path = instance_dir / "memory" / "staging" / f"self-model-{from_state}.jsonl"
    to_path = instance_dir / "memory" / "staging" / f"self-model-{to_state}.jsonl"

    if not from_path.exists():
        return

    to_path.parent.mkdir(parents=True, exist_ok=True)
    lines = from_path.read_text(encoding="utf-8").splitlines()
    remaining = []
    moved = None

    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            if data.get("id") == proposal_id:
                moved = line
            else:
                remaining.append(line)
        except json.JSONDecodeError:
            remaining.append(line)

    if moved:
        with to_path.open("a", encoding="utf-8") as f:
            f.write(moved + "\n")
        from_path.write_text("\n".join(remaining) + ("\n" if remaining else ""))


def count_proposals(instance_dir: Path, state: str = "staging") -> int:
    """Count proposals in a state."""
    return sum(1 for _ in load_proposals(instance_dir, state))
