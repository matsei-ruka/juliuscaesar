"""Tests for lib/persona_interview/splice.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from persona_interview.questions import Prompt, Slot, Validation  # noqa: E402
from persona_interview.splice import SpliceError, splice_slot_body  # noqa: E402


def _slot(slot_id="rules.foo", target_section="## §2 — FOO") -> Slot:
    return Slot(
        slot_id=slot_id,
        target_file="memory/L1/RULES.md",
        target_section=target_section,
        placeholder=f"{{{{slot:{slot_id}}}}}",
        applicability=("always",),
        kind="text",
        status="exemplar",
        prompts=(Prompt(id="q", text="?", kind="text", validation=Validation()),),
        composition=None,
    )


def _build_file(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "memory" / "L1" / "RULES.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def test_splice_replaces_placeholder(tmp_path):
    _build_file(tmp_path, """# RULES

## §2 — FOO
<!-- REVIEWABLE -->

<!-- ASK: explain foo -->
{{slot:rules.foo}}

## §3 — BAR

other body
""")
    splice_slot_body(tmp_path, _slot(), "Composed foo content.\n")
    text = (tmp_path / "memory/L1/RULES.md").read_text(encoding="utf-8")
    assert "{{slot:" not in text
    assert "Composed foo content." in text
    assert "<!-- ASK:" not in text     # ASK comment dropped
    assert "<!-- REVIEWABLE -->" in text  # marker preserved
    assert "## §3 — BAR" in text       # next section untouched
    assert "other body" in text


def test_splice_preserves_marker(tmp_path):
    _build_file(tmp_path, """## §2 — FOO
<!-- OPEN -->

{{slot:rules.foo}}
""")
    splice_slot_body(tmp_path, _slot(), "X\n")
    text = (tmp_path / "memory/L1/RULES.md").read_text(encoding="utf-8")
    assert "<!-- OPEN -->" in text


def test_splice_creates_section_if_missing(tmp_path):
    _build_file(tmp_path, "# RULES\n\nstub\n")
    splice_slot_body(tmp_path, _slot(), "Composed.\n", create_if_missing=True)
    text = (tmp_path / "memory/L1/RULES.md").read_text(encoding="utf-8")
    assert "## §2 — FOO" in text
    assert "Composed." in text


def test_splice_refuses_when_section_missing_and_create_false(tmp_path):
    _build_file(tmp_path, "# RULES\n")
    with pytest.raises(SpliceError, match="section not found"):
        splice_slot_body(tmp_path, _slot(), "X\n", create_if_missing=False)


def test_splice_refuses_when_target_file_missing(tmp_path):
    with pytest.raises(SpliceError, match="target file not found"):
        splice_slot_body(tmp_path, _slot(), "X\n")


def test_splice_creates_backup(tmp_path):
    target = _build_file(tmp_path, "## §2 — FOO\n\n{{slot:rules.foo}}\n")
    prior = target.read_text(encoding="utf-8")
    splice_slot_body(tmp_path, _slot(), "New body.\n")
    redo_root = tmp_path / "state" / "persona" / "redo"
    assert redo_root.exists()
    backups = list(redo_root.glob("*/*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == prior


def test_splice_no_op_when_already_correct(tmp_path):
    """Splicing the same body twice should be a no-op the second time (no backup churn)."""
    _build_file(tmp_path, "## §2 — FOO\n\n{{slot:rules.foo}}\n")
    splice_slot_body(tmp_path, _slot(), "Done.\n")
    backups_before = list((tmp_path / "state").rglob("*.bak"))
    splice_slot_body(tmp_path, _slot(), "Done.\n")
    backups_after = list((tmp_path / "state").rglob("*.bak"))
    # Second run produced no NEW backup since text was unchanged.
    assert len(backups_after) == len(backups_before)
