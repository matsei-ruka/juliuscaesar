"""Synthesize proposals from detected signals via LLM, with frozen-section guards."""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import frozen_sections
from .conf import SelfModelConfig
from .detector import Signal
from .store import Proposal, content_hash, has_recent_proposal


logger = logging.getLogger("self_model.proposer")


# Heuristic: text excerpts containing any of these strings probably target an
# IMMUTABILE constitutional surface; drop the signal pre-LLM rather than risk
# the LLM emitting a doomed proposal.
_PRE_LLM_FROZEN_HINTS = [
    "§1 ", "§11 ", "§14 ", "§16 ", "§18 ", "§19 ", "§21 ",
    "TRUST MODEL",
    "MEMORY ACCESS CONTROL",
    "AZIONI A DOPPIO BLOCCO",
    "SELF-CHECK FINALE",
    "PRINCIPIO FINALE",
    "ANTI-SUBMISSION",
    "HARD RULE",
    "HARD NO",
    "Stato AI",
    "Riservatezza ruolo",
    "Obiettivo gerarchico",
    "Principio supremo",
]


def generate_proposals(
    instance_dir: Path,
    signals: list[Signal],
    config: SelfModelConfig,
    current_rules_md: str,
) -> Iterator[Proposal]:
    """Aggregate signals + call LLM to generate proposals. Drop frozen-section ones."""
    if not signals:
        return

    filtered = _prefilter_signals(signals)
    if not filtered:
        return

    prompt = _build_proposer_prompt(filtered, current_rules_md, config)
    response_text = _call_proposer_llm(prompt, config)

    try:
        proposals_data = json.loads(response_text)
        if not isinstance(proposals_data, list):
            proposals_data = [proposals_data]
    except json.JSONDecodeError:
        return

    for i, proposal_data in enumerate(proposals_data):
        try:
            assert proposal_data.get("target_file")
            assert proposal_data.get("proposed_content")
            assert proposal_data.get("reasoning")

            target_file = proposal_data["target_file"]
            target_section = proposal_data.get("target_section")

            # Post-LLM frozen-section guard.
            if frozen_sections.is_section_frozen(target_file, target_section):
                logger.warning(
                    "dropping proposal targeting IMMUTABILE section: %s / %s",
                    target_file, target_section,
                )
                continue

            # HTML marker guard — read the target file and check for IMMUTABILE marker.
            if _section_marker_immutable_in_file(instance_dir, target_file, target_section):
                logger.warning(
                    "dropping proposal — section has <!-- IMMUTABILE --> marker: %s / %s",
                    target_file, target_section,
                )
                continue

            hash_val = content_hash(
                target_file,
                target_section,
                proposal_data["proposed_content"],
            )

            if has_recent_proposal(instance_dir, hash_val, config.proposal_cooldown_days):
                continue

            proposal = Proposal(
                id=f"{datetime.now(timezone.utc).strftime('%Y%m%d')}-{hash(str(i)) & 0xffffff:06x}",
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                type=proposal_data.get("type", "modify"),
                target_file=target_file,
                target_section=target_section,
                current_content=proposal_data.get("current_content", ""),
                proposed_content=proposal_data["proposed_content"],
                reasoning=proposal_data["reasoning"],
                confidence=float(proposal_data.get("confidence", 0.5)),
                supporting_evidence=proposal_data.get("supporting_evidence", []),
                content_hash=hash_val,
            )
            yield proposal
        except (AssertionError, KeyError, ValueError):
            continue


def _prefilter_signals(signals: list[Signal]) -> list[Signal]:
    """Drop signals whose text_excerpt clearly points at IMMUTABILE surfaces."""
    out: list[Signal] = []
    for s in signals:
        excerpt = (s.text_excerpt or "")
        if any(hint in excerpt for hint in _PRE_LLM_FROZEN_HINTS):
            logger.warning(
                "pre-LLM drop: signal %s/%s likely targets IMMUTABILE surface",
                s.kind, s.trigger,
            )
            continue
        out.append(s)
    return out


def _section_marker_immutable_in_file(
    instance_dir: Path,
    target_file: str,
    target_section: str | None,
) -> bool:
    """Read target file and check for IMMUTABILE marker under heading."""
    if not target_section:
        return False
    target_path = instance_dir / target_file
    if not target_path.exists():
        return False
    try:
        content = target_path.read_text(encoding="utf-8")
    except OSError:
        return False
    import re as _re
    match = _re.search(_re.escape(target_section), content)
    if not match:
        return False
    after = content[match.end():].splitlines()
    seen = 0
    for line in after:
        stripped = line.strip()
        if not stripped:
            continue
        if frozen_sections.MARKER_IMMUTABILE in stripped:
            return True
        seen += 1
        if seen >= 3:
            break
    return False


def _build_proposer_prompt(
    signals: list[Signal],
    rules_md: str,
    config: SelfModelConfig,
) -> str:
    """Build LLM prompt to synthesize signals into proposals."""
    signals_text = json.dumps(
        [
            {
                "kind": s.kind,
                "trigger": s.trigger,
                "text_excerpt": s.text_excerpt,
                "conversation_id": s.conversation_id,
                "ts": s.ts,
                "severity": s.severity,
                "evidence": s.evidence,
            }
            for s in signals
        ],
        indent=2,
    )

    return f"""You are analyzing the agent's own behavioral patterns to suggest updates to its operative files.

Current RULES.md (behavioural constitution):
```
{rules_md}
```

Detected self-observation signals:
{signals_text}

Task: Synthesize these signals into 1-3 JSON proposals. Targets allowed:
- "memory/L1/JOURNAL.md" — append a new entry (auto-apply scope)
- "memory/L1/RULES.md" — modify a non-IMMUTABILE section (requires DKIM email approval)
- "memory/L1/IDENTITY.md" — modify a non-IMMUTABILE section (requires DKIM email approval)

For each proposal, output a JSON object with:
- type: "add" | "modify" | "remove"
- target_file: one of the three above
- target_section: heading text (e.g. "## Entries") or null for top-level
- current_content: exact text to match (for modify/remove)
- proposed_content: new/updated content
- reasoning: why this change (1-2 sentences)
- confidence: 0.0-1.0
- supporting_evidence: list of signal kinds supporting this proposal

CRITICAL constraints:
- Do NOT propose changes to sections marked IMMUTABILE in RULES.md or IDENTITY.md
  (constitutional invariants — only Filippo via DKIM email can modify).
- Prefer JOURNAL.md append for behavioural observations; only escalate to RULES.md
  when a clear pattern across ≥2 distinct evidence items justifies a rule change.

Return a JSON array of proposals. Do NOT include proposals with confidence < 0.5.
"""


def _call_proposer_llm(prompt: str, config: SelfModelConfig) -> str:
    """Call LLM to generate proposals. Return JSON response."""
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
