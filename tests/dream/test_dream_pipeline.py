from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from dream.apply import apply_artifacts, reject  # noqa: E402
from dream.runner import run_dream  # noqa: E402
from dream.schema import ProposedArtifact  # noqa: E402
from gateway import transcripts  # noqa: E402


def _make_instance(root: Path) -> None:
    (root / ".jc").write_text("framework=juliuscaesar\n", encoding="utf-8")
    (root / "memory" / "L1").mkdir(parents=True)
    (root / "memory" / "L2" / "projects").mkdir(parents=True)
    (root / "memory" / "L2" / "people").mkdir(parents=True)
    (root / "heartbeat").mkdir()
    (root / "memory" / "L1" / "RULES.md").write_text("# RULES\n", encoding="utf-8")
    (root / "memory" / "L1" / "HOT.md").write_text(
        "# Hot\n\n## Pattern #self-observation\nI missed a repeated follow-up.\n",
        encoding="utf-8",
    )
    (root / "memory" / "L2" / "projects" / "alpha.md").write_text(
        """---
slug: projects/alpha
title: Alpha
layer: L2
type: project
state: active
created: 2025-01-01
updated: 2025-01-01
last_verified: 2025-01-01
tags: [project]
links: []
---

# Alpha

Alice is CEO. Link to [[missing-topic]].
""",
        encoding="utf-8",
    )
    transcripts.append(
        root,
        conversation_id="123",
        role="user",
        text="review yourself on this pattern",
        channel="telegram",
        chat_id="123",
        ts="2026-05-11T10:00:00+00:00",
    )
    transcripts.append(
        root,
        conversation_id="123",
        role="assistant",
        text="I missed the follow-up; drift mio.",
        channel="telegram",
        chat_id="123",
        ts="2026-05-11T10:01:00+00:00",
    )


def test_synthetic_dream_writes_report_and_artifacts(tmp_path: Path) -> None:
    _make_instance(tmp_path)

    result = run_dream(tmp_path, until=datetime(2026, 5, 12, 3, 30, tzinfo=timezone.utc))

    assert result.status == "completed"
    assert result.report_path is not None and result.report_path.exists()
    report = result.report_path.read_text(encoding="utf-8")
    assert "## Reflection summary" in report
    assert "AUTO_APPLIED" in report
    assert list((tmp_path / "memory" / "L2" / "playbooks").glob("*.md"))
    assert list((tmp_path / "memory" / "L2" / "learnings").glob("*.md"))
    assert (tmp_path / "memory" / "L2" / "stubs" / "missing-topic.md").exists()
    assert list((tmp_path / "state" / "commitments").glob("*.yaml"))


def test_dream_dry_run_does_not_write(tmp_path: Path) -> None:
    _make_instance(tmp_path)

    result = run_dream(
        tmp_path,
        until=datetime(2026, 5, 12, 3, 30, tzinfo=timezone.utc),
        dry_run=True,
    )

    assert result.report_path is None
    assert not (tmp_path / "state" / "dreams" / "20260512T033000Z.md").exists()
    assert not (tmp_path / "memory" / "L2" / "playbooks").exists()


def test_reject_rolls_back_auto_applied_artifact(tmp_path: Path) -> None:
    artifact = ProposedArtifact(
        diff_id="dream-test-rollback",
        kind="learning",
        risk_class="MEDIUM",
        path="memory/L2/learnings/test.md",
        title="Test",
        content="# Test\n",
    )

    applied = apply_artifacts(tmp_path, [artifact])

    assert applied[0].status == "AUTO_APPLIED"
    assert (tmp_path / "memory" / "L2" / "learnings" / "test.md").exists()
    assert reject(tmp_path, "dream-test-rollback") == "auto-applied diff rolled back"
    assert not (tmp_path / "memory" / "L2" / "learnings" / "test.md").exists()


def test_sensitive_artifact_stages_for_approval(tmp_path: Path) -> None:
    artifact = ProposedArtifact(
        diff_id="dream-sensitive",
        kind="rules_proposal",
        risk_class="SENSITIVE",
        path="memory/L1/RULES.md",
        title="Rules proposal",
        content="# RULES\nnew\n",
    )

    applied = apply_artifacts(tmp_path, [artifact])

    assert applied[0].status == "STAGED"
    path = tmp_path / "state" / "dreams" / "pending" / "dream-sensitive.json"
    assert json.loads(path.read_text(encoding="utf-8"))["artifact"]["path"] == "memory/L1/RULES.md"
