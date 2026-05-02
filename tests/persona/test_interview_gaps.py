"""Tests for lib/persona_interview/gaps.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from persona_interview.gaps import GapState, classify_slot, find_gaps, summarize  # noqa: E402
from persona_interview.questions import Prompt, QuestionsBank, Slot, Validation  # noqa: E402


def _slot(slot_id="rules.foo", target_file="memory/L1/RULES.md",
          target_section="## §2 — FOO", placeholder=None) -> Slot:
    placeholder = placeholder or f"{{{{slot:{slot_id}}}}}"
    return Slot(
        slot_id=slot_id,
        target_file=target_file,
        target_section=target_section,
        placeholder=placeholder,
        applicability=("always",),
        kind="text",
        status="exemplar",
        prompts=(Prompt(id="q", text="?", kind="text", validation=Validation(required=True)),),
        composition=None,
    )


def test_classify_missing_file(tmp_path):
    slot = _slot()
    gap = classify_slot(tmp_path, slot)
    assert gap.state == GapState.MISSING


def test_classify_missing_section(tmp_path):
    (tmp_path / "memory" / "L1").mkdir(parents=True)
    (tmp_path / "memory/L1/RULES.md").write_text("# RULES\n\n## §99 — OTHER\n\nbody\n", encoding="utf-8")
    gap = classify_slot(tmp_path, _slot())
    assert gap.state == GapState.MISSING


def test_classify_unfilled_with_placeholder(tmp_path):
    (tmp_path / "memory" / "L1").mkdir(parents=True)
    (tmp_path / "memory/L1/RULES.md").write_text(
        "# RULES\n\n## §2 — FOO\n<!-- REVIEWABLE -->\n\n{{slot:rules.foo}}\n", encoding="utf-8",
    )
    gap = classify_slot(tmp_path, _slot())
    assert gap.state == GapState.UNFILLED


def test_classify_unfilled_when_section_only_has_marker_and_ask(tmp_path):
    """Heuristic: marker + ASK comment with no body should count as unfilled."""
    (tmp_path / "memory" / "L1").mkdir(parents=True)
    (tmp_path / "memory/L1/RULES.md").write_text(
        "# RULES\n\n## §2 — FOO\n<!-- REVIEWABLE -->\n\n<!-- ASK: fill me -->\n", encoding="utf-8",
    )
    gap = classify_slot(tmp_path, _slot(placeholder="{{slot:rules.foo-not-present}}"))
    assert gap.state == GapState.UNFILLED


def test_classify_populated(tmp_path):
    (tmp_path / "memory" / "L1").mkdir(parents=True)
    (tmp_path / "memory/L1/RULES.md").write_text(
        "# RULES\n\n## §2 — FOO\n<!-- REVIEWABLE -->\n\nReal authored body.\n",
        encoding="utf-8",
    )
    gap = classify_slot(tmp_path, _slot())
    assert gap.state == GapState.POPULATED


def test_find_gaps_skips_populated_by_default(tmp_path):
    (tmp_path / "memory" / "L1").mkdir(parents=True)
    (tmp_path / "memory/L1/RULES.md").write_text(
        "# RULES\n\n## §2 — FOO\n\nReal body.\n\n## §3 — BAR\n\n{{slot:rules.bar}}\n",
        encoding="utf-8",
    )
    bank = QuestionsBank(version=1, slots=(
        _slot("rules.foo", "memory/L1/RULES.md", "## §2 — FOO"),
        _slot("rules.bar", "memory/L1/RULES.md", "## §3 — BAR"),
    ))
    gaps = find_gaps(tmp_path, bank)
    assert [g.slot.slot_id for g in gaps] == ["rules.bar"]


def test_find_gaps_include_populated(tmp_path):
    (tmp_path / "memory" / "L1").mkdir(parents=True)
    (tmp_path / "memory/L1/RULES.md").write_text(
        "# RULES\n\n## §2 — FOO\n\nReal body.\n\n## §3 — BAR\n\n{{slot:rules.bar}}\n",
        encoding="utf-8",
    )
    bank = QuestionsBank(version=1, slots=(
        _slot("rules.foo", "memory/L1/RULES.md", "## §2 — FOO"),
        _slot("rules.bar", "memory/L1/RULES.md", "## §3 — BAR"),
    ))
    gaps = find_gaps(tmp_path, bank, include_populated=True)
    assert {g.slot.slot_id for g in gaps} == {"rules.foo", "rules.bar"}


def test_summarize_counts(tmp_path):
    bank = QuestionsBank(version=1, slots=(
        _slot("rules.foo"),
    ))
    gaps = find_gaps(tmp_path, bank)
    s = summarize(gaps)
    assert s["missing"] == 1
    assert s["unfilled"] == 0
    assert s["populated"] == 0
    assert s["total"] == 1
