"""Tests for lib/self_model/store.py — proposal persistence + dedup."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from self_model.store import (  # noqa: E402
    Proposal,
    content_hash,
    count_proposals,
    has_recent_proposal,
    load_proposals,
    move_proposal,
    save_proposal,
)


def _proposal(target_file="memory/L1/JOURNAL.md", content="body", id_="p1") -> Proposal:
    return Proposal(
        id=id_,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        type="modify",
        target_file=target_file,
        target_section="## Entries",
        current_content="",
        proposed_content=content,
        reasoning="test",
        confidence=0.9,
        supporting_evidence=["s1"],
        content_hash=content_hash(target_file, "## Entries", content),
    )


def test_content_hash_is_deterministic():
    a = content_hash("memory/L1/RULES.md", "## §2", "body")
    b = content_hash("memory/L1/RULES.md", "## §2", "body")
    assert a == b
    assert a.startswith("sha256:")


def test_content_hash_differs_with_content():
    a = content_hash("memory/L1/RULES.md", "## §2", "body one")
    b = content_hash("memory/L1/RULES.md", "## §2", "body two")
    assert a != b


def test_save_and_load_proposal(tmp_path: Path):
    save_proposal(tmp_path, _proposal())
    loaded = list(load_proposals(tmp_path))
    assert len(loaded) == 1
    assert loaded[0].id == "p1"
    assert loaded[0].type == "modify"


def test_count_proposals_per_state(tmp_path: Path):
    save_proposal(tmp_path, _proposal(id_="p1"), state="staging")
    save_proposal(tmp_path, _proposal(id_="p2"), state="staging")
    save_proposal(tmp_path, _proposal(id_="p3"), state="applied")
    assert count_proposals(tmp_path, "staging") == 2
    assert count_proposals(tmp_path, "applied") == 1
    assert count_proposals(tmp_path, "rejected") == 0


def test_move_proposal_staging_to_applied(tmp_path: Path):
    save_proposal(tmp_path, _proposal(id_="p1"))
    move_proposal(tmp_path, "p1", "staging", "applied")
    assert count_proposals(tmp_path, "staging") == 0
    assert count_proposals(tmp_path, "applied") == 1


def test_has_recent_proposal_dedup(tmp_path: Path):
    p = _proposal()
    save_proposal(tmp_path, p)
    assert has_recent_proposal(tmp_path, p.content_hash, cooldown_days=30)
    # Different hash → not duplicated.
    assert not has_recent_proposal(tmp_path, "sha256:nonexistent", cooldown_days=30)
