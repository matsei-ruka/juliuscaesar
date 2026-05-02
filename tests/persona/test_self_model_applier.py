"""Tests for lib/self_model/applier.py.

Locks the IMMUTABILE-section guards: pre-check via the registry, HTML-marker
re-check at apply time, path-escape security, JOURNAL auto-apply scope.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from self_model.applier import (  # noqa: E402
    ApplierError,
    apply_proposal,
)
from self_model.store import Proposal, content_hash


def _proposal(
    target_file: str,
    target_section: str,
    proposed_content: str = "new body",
    type_: str = "modify",
    current_content: str = "",
) -> Proposal:
    return Proposal(
        id="test-p1",
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        type=type_,
        target_file=target_file,
        target_section=target_section,
        current_content=current_content,
        proposed_content=proposed_content,
        reasoning="test",
        confidence=0.9,
        supporting_evidence=[],
        content_hash=content_hash(target_file, target_section, proposed_content),
    )


def _build_instance(tmp_path: Path) -> Path:
    """Build a minimal instance dir with memory/L1/{RULES,JOURNAL}.md."""
    (tmp_path / "memory" / "L1").mkdir(parents=True)
    (tmp_path / "memory" / "L1" / "RULES.md").write_text(
        "## §0 — AI TRANSPARENCY DOCTRINE\n<!-- IMMUTABILE -->\n\nDoctrine.\n\n"
        "## §2 — OPERATING MODES\n<!-- REVIEWABLE -->\n\nOriginal body to modify.\n",
        encoding="utf-8",
    )
    (tmp_path / "memory" / "L1" / "JOURNAL.md").write_text(
        "# JOURNAL\n\n## Entries\n\n",
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# IMMUTABILE guard — registry-based
# ---------------------------------------------------------------------------

def test_apply_rejects_immutable_section_via_registry(tmp_path: Path):
    instance = _build_instance(tmp_path)
    proposal = _proposal(
        "memory/L1/RULES.md",
        "## §0 — AI TRANSPARENCY DOCTRINE",
        proposed_content="new doctrine body",
        current_content="Doctrine.",
    )
    with pytest.raises(ApplierError, match="IMMUTABILE|DKIM"):
        apply_proposal(instance, proposal)


def test_apply_rejects_immutable_section_via_html_marker(tmp_path: Path):
    """Even if a section isn't in the registry, an inline IMMUTABILE marker blocks it."""
    instance = tmp_path
    (instance / "memory" / "L1").mkdir(parents=True)
    (instance / "memory" / "L1" / "RULES.md").write_text(
        "## §99 — NEW DOCTRINE\n<!-- IMMUTABILE -->\n\nBody.\n",
        encoding="utf-8",
    )
    proposal = _proposal(
        "memory/L1/RULES.md",
        "## §99 — NEW DOCTRINE",
        proposed_content="evil",
        current_content="Body.",
    )
    with pytest.raises(ApplierError, match="IMMUTABILE|DKIM"):
        apply_proposal(instance, proposal)


# ---------------------------------------------------------------------------
# Path-escape security
# ---------------------------------------------------------------------------

def test_apply_rejects_path_escape(tmp_path: Path):
    instance = _build_instance(tmp_path)
    # Force a path outside memory/.
    (tmp_path / "evil.md").write_text("y", encoding="utf-8")
    proposal = _proposal(
        "../evil.md",
        "## anything",
        proposed_content="x",
    )
    with pytest.raises(ApplierError, match="path escape|target file not found"):
        apply_proposal(instance, proposal)


# ---------------------------------------------------------------------------
# JOURNAL auto-apply scope (DKIM gate skipped)
# ---------------------------------------------------------------------------

def test_journal_append_works_without_dkim(tmp_path: Path):
    """JOURNAL.md is auto-apply scope — no DKIM gate, no frozen-section check."""
    instance = _build_instance(tmp_path)
    proposal = _proposal(
        "memory/L1/JOURNAL.md",
        "## Entries",
        proposed_content="\n## 2026-05-01 14:00 — test-entry\n**Trigger:** episode_flag\n**State:** open\n",
        type_="add",
    )
    apply_proposal(instance, proposal)
    body = (instance / "memory/L1/JOURNAL.md").read_text(encoding="utf-8")
    assert "test-entry" in body


def test_journal_modify_creates_backup(tmp_path: Path):
    """Apply on JOURNAL.md should snapshot prior content to memory/.history/."""
    instance = _build_instance(tmp_path)
    journal = instance / "memory/L1/JOURNAL.md"
    proposal = _proposal(
        "memory/L1/JOURNAL.md",
        "## Entries",
        proposed_content="\n## 2026-05-01 14:00 — test\n**State:** open\n",
        type_="add",
    )
    apply_proposal(instance, proposal)
    history = instance / "memory" / ".history"
    assert history.exists()
    backups = list(history.glob("JOURNAL.md.*"))
    assert len(backups) >= 1


# ---------------------------------------------------------------------------
# DKIM gate stub — currently returns False; non-JOURNAL targets must error.
# ---------------------------------------------------------------------------

def test_non_journal_modify_rejected_when_dkim_stubbed(tmp_path: Path):
    """Until DKIM is implemented, ALL non-JOURNAL applies should fail with a
    clear DKIM-missing error. This protects RULES/IDENTITY by default."""
    instance = _build_instance(tmp_path)
    proposal = _proposal(
        "memory/L1/RULES.md",
        "## §2 — OPERATING MODES",  # not in DOCTRINE_SECTIONS, but still non-JOURNAL
        proposed_content="modes body",
        current_content="Original body to modify.",
    )
    with pytest.raises(ApplierError, match="DKIM"):
        apply_proposal(instance, proposal)
