"""Synthesize proposals from detected signals via LLM."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .conf import UserModelConfig
from .detector import Signal
from .store import Proposal, content_hash, has_recent_proposal


def generate_proposals(
    instance_dir: Path,
    signals: list[Signal],
    config: UserModelConfig,
    current_user_md: str,
) -> Iterator[Proposal]:
    """Aggregate signals + call LLM to generate proposals. Return non-duplicate ones."""
    if not signals:
        return

    # Call LLM to synthesize signals into structured proposals.
    prompt = _build_proposer_prompt(signals, current_user_md, config)
    response_text = _call_proposer_llm(prompt, config)

    try:
        proposals_data = json.loads(response_text)
        if not isinstance(proposals_data, list):
            proposals_data = [proposals_data]
    except json.JSONDecodeError:
        # LLM didn't return valid JSON — log and skip.
        return

    for i, proposal_data in enumerate(proposals_data):
        try:
            # Validate proposal shape.
            assert proposal_data.get("target_file")
            assert proposal_data.get("proposed_content")
            assert proposal_data.get("reasoning")

            hash_val = content_hash(
                proposal_data["target_file"],
                proposal_data.get("target_section"),
                proposal_data["proposed_content"],
            )

            # Check cooldown + dedup.
            if has_recent_proposal(instance_dir, hash_val, config.proposal_cooldown_days):
                continue

            proposal = Proposal(
                id=f"{datetime.now(timezone.utc).strftime('%Y%m%d')}-{hash(str(i)) & 0xffffff:06x}",
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                type=proposal_data.get("type", "modify"),
                target_file=proposal_data["target_file"],
                target_section=proposal_data.get("target_section"),
                current_content=proposal_data.get("current_content", ""),
                proposed_content=proposal_data["proposed_content"],
                reasoning=proposal_data["reasoning"],
                confidence=float(proposal_data.get("confidence", 0.5)),
                supporting_evidence=proposal_data.get("supporting_evidence", []),
                content_hash=hash_val,
            )
            yield proposal
        except (AssertionError, KeyError, ValueError):
            # Invalid proposal — skip.
            continue


def _build_proposer_prompt(signals: list[Signal], user_md: str, config: UserModelConfig) -> str:
    """Build LLM prompt to synthesize signals into proposals."""
    signals_text = json.dumps(
        [
            {
                "kind": s.kind,
                "term": s.term,
                "count": s.count,
                "prev_count": s.prev_count,
                "curr_count": s.curr_count,
                "dimension": s.dimension,
                "rule_excerpt": s.rule_excerpt,
                "severity": s.severity,
            }
            for s in signals
        ],
        indent=2,
    )

    return f"""You are analyzing conversation patterns to suggest updates to a user profile.

Current USER.md:
```
{user_md}
```

Detected signals:
{signals_text}

Task: Synthesize these signals into 1-3 JSON proposals to update the USER.md file.

For each proposal, output a JSON object with:
- type: "add" | "modify" | "remove"
- target_file: "memory/L1/USER.md"
- target_section: heading like "## Family" or null for top-level
- current_content: exact text to match in USER.md (for modify/remove)
- proposed_content: new/updated content
- reasoning: why this change (1-2 sentences)
- confidence: 0.0-1.0 (0.85+ for auto-apply, below for manual review)
- supporting_evidence: list of signal kinds supporting this proposal

Return a JSON array of proposals. Do NOT include proposals with confidence < 0.5.
"""


def _call_proposer_llm(prompt: str, config: UserModelConfig) -> str:
    """Call LLM to generate proposals. Return JSON response."""
    # Use claude CLI for deterministic structured output.
    proc = subprocess.run(
        ["claude", "-p", "--model", config.proposer_model, "--dangerously-skip-permissions"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"proposer LLM failed: {proc.stderr}")
    return proc.stdout
