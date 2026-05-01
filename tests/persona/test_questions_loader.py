"""Tests for lib/persona_interview/questions.py — the YAML question-bank loader."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from persona_interview.questions import (  # noqa: E402
    Composition,
    Dependency,
    Prompt,
    QuestionsBank,
    QuestionsBankError,
    Slot,
    Validation,
    load_questions,
)


def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "questions.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# The shipped questions.yaml is internally consistent
# ---------------------------------------------------------------------------

def test_real_questions_yaml_loads():
    framework_root = Path(__file__).resolve().parent.parent.parent
    bank = load_questions(framework_root / "templates" / "persona-interview" / "questions.yaml")
    assert isinstance(bank, QuestionsBank)
    assert bank.version == 1
    assert len(bank.slots) > 0


def test_every_slot_in_overrides_has_a_questions_entry():
    """Every entry in slot-overrides.yaml that has a slot_id must appear in questions.yaml."""
    import yaml as pyyaml
    framework_root = Path(__file__).resolve().parent.parent.parent
    overrides_path = framework_root / "templates" / "persona-interview" / "slot-overrides.yaml"
    overrides = pyyaml.safe_load(overrides_path.read_text(encoding="utf-8"))

    expected_slot_ids: set[str] = set()
    for file_overrides in (overrides.get("sections") or {}).values():
        for entry in file_overrides.values():
            if isinstance(entry, dict) and "slot_id" in entry:
                expected_slot_ids.add(entry["slot_id"])

    bank = load_questions(framework_root / "templates" / "persona-interview" / "questions.yaml")
    actual = {s.slot_id for s in bank.slots}

    missing = expected_slot_ids - actual
    assert not missing, f"questions.yaml is missing entries for: {sorted(missing)}"


def test_real_questions_has_at_least_one_exemplar_per_file():
    """Every file the framework ships should have ≥1 exemplar slot to demonstrate the pattern."""
    framework_root = Path(__file__).resolve().parent.parent.parent
    bank = load_questions(framework_root / "templates" / "persona-interview" / "questions.yaml")
    files = {s.target_file for s in bank.slots}
    for f in files:
        slots = bank.for_file(f)
        exemplars = [s for s in slots if s.status == "exemplar"]
        assert exemplars, f"file {f} has no exemplar slot"


# ---------------------------------------------------------------------------
# Schema validation — the loader rejects malformed input cleanly
# ---------------------------------------------------------------------------

def test_top_level_must_be_mapping(tmp_path):
    p = _write_yaml(tmp_path, "- just a list\n")
    with pytest.raises(QuestionsBankError, match="top-level"):
        load_questions(p)


def test_missing_version(tmp_path):
    p = _write_yaml(tmp_path, "slots: []\n")
    with pytest.raises(QuestionsBankError, match="version"):
        load_questions(p)


def test_empty_slots_rejected(tmp_path):
    p = _write_yaml(tmp_path, "version: 1\nslots: []\n")
    with pytest.raises(QuestionsBankError, match="non-empty"):
        load_questions(p)


def test_duplicate_slot_id_rejected(tmp_path):
    p = _write_yaml(tmp_path, """
version: 1
slots:
  - slot_id: foo
    target_file: x.md
    target_section: "## X"
    placeholder: "{{slot:foo}}"
    applicability: [always]
    kind: text
    prompts:
      - id: a
        text: "Q?"
  - slot_id: foo
    target_file: y.md
    target_section: "## Y"
    placeholder: "{{slot:foo}}"
    applicability: [always]
    kind: text
    prompts:
      - id: b
        text: "Q?"
""")
    with pytest.raises(QuestionsBankError, match="duplicate slot_id"):
        load_questions(p)


def test_invalid_kind_rejected(tmp_path):
    p = _write_yaml(tmp_path, """
version: 1
slots:
  - slot_id: foo
    target_file: x.md
    target_section: "## X"
    placeholder: "{{slot:foo}}"
    applicability: [always]
    kind: bogus
    prompts:
      - id: a
        text: "Q?"
""")
    with pytest.raises(QuestionsBankError, match="bogus"):
        load_questions(p)


def test_choice_prompt_requires_choices(tmp_path):
    p = _write_yaml(tmp_path, """
version: 1
slots:
  - slot_id: foo
    target_file: x.md
    target_section: "## X"
    placeholder: "{{slot:foo}}"
    applicability: [always]
    kind: choice
    prompts:
      - id: a
        text: "Pick one"
        kind: choice
""")
    with pytest.raises(QuestionsBankError, match="non-empty 'choices'"):
        load_questions(p)


def test_depends_on_must_reference_existing_prompt(tmp_path):
    p = _write_yaml(tmp_path, """
version: 1
slots:
  - slot_id: foo
    target_file: x.md
    target_section: "## X"
    placeholder: "{{slot:foo}}"
    applicability: [always]
    kind: structured
    prompts:
      - id: a
        text: "First"
      - id: b
        text: "Second"
        depends_on: "nonexistent == yes"
""")
    with pytest.raises(QuestionsBankError, match="unknown id 'nonexistent'"):
        load_questions(p)


def test_depends_on_syntax_validated(tmp_path):
    p = _write_yaml(tmp_path, """
version: 1
slots:
  - slot_id: foo
    target_file: x.md
    target_section: "## X"
    placeholder: "{{slot:foo}}"
    applicability: [always]
    kind: structured
    prompts:
      - id: a
        text: "First"
      - id: b
        text: "Second"
        depends_on: "a equals yes"
""")
    with pytest.raises(QuestionsBankError, match="does not match"):
        load_questions(p)


def test_composition_only_on_structured(tmp_path):
    p = _write_yaml(tmp_path, """
version: 1
slots:
  - slot_id: foo
    target_file: x.md
    target_section: "## X"
    placeholder: "{{slot:foo}}"
    applicability: [always]
    kind: text
    prompts:
      - id: a
        text: "Q?"
    composition:
      template: "{{a}}"
""")
    with pytest.raises(QuestionsBankError, match="kind=structured"):
        load_questions(p)


def test_composition_template_references_unknown_prompt(tmp_path):
    p = _write_yaml(tmp_path, """
version: 1
slots:
  - slot_id: foo
    target_file: x.md
    target_section: "## X"
    placeholder: "{{slot:foo}}"
    applicability: [always]
    kind: structured
    prompts:
      - id: a
        text: "Q?"
    composition:
      template: |
        {{a}} and {{b}}
""")
    with pytest.raises(QuestionsBankError, match="unknown prompt ids"):
        load_questions(p)


def test_invalid_regex_pattern_rejected(tmp_path):
    p = _write_yaml(tmp_path, """
version: 1
slots:
  - slot_id: foo
    target_file: x.md
    target_section: "## X"
    placeholder: "{{slot:foo}}"
    applicability: [always]
    kind: text
    prompts:
      - id: a
        text: "Q?"
        validation:
          pattern: "[unclosed"
""")
    with pytest.raises(QuestionsBankError, match="not valid regex"):
        load_questions(p)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def test_find_returns_slot_or_none(tmp_path):
    p = _write_yaml(tmp_path, """
version: 1
slots:
  - slot_id: alpha
    target_file: a.md
    target_section: "## A"
    placeholder: "{{slot:alpha}}"
    applicability: [always]
    kind: text
    prompts:
      - id: q
        text: "Q?"
""")
    bank = load_questions(p)
    assert bank.find("alpha").slot_id == "alpha"
    assert bank.find("missing") is None


def test_for_file_filters(tmp_path):
    p = _write_yaml(tmp_path, """
version: 1
slots:
  - slot_id: a
    target_file: file_a.md
    target_section: "## A"
    placeholder: "{{slot:a}}"
    applicability: [always]
    kind: text
    prompts:
      - id: q
        text: "Q?"
  - slot_id: b
    target_file: file_b.md
    target_section: "## B"
    placeholder: "{{slot:b}}"
    applicability: [always]
    kind: text
    prompts:
      - id: q
        text: "Q?"
  - slot_id: c
    target_file: file_a.md
    target_section: "## C"
    placeholder: "{{slot:c}}"
    applicability: [always]
    kind: text
    prompts:
      - id: q
        text: "Q?"
""")
    bank = load_questions(p)
    in_a = bank.for_file("file_a.md")
    assert {s.slot_id for s in in_a} == {"a", "c"}
    assert bank.for_file("missing.md") == ()


# ---------------------------------------------------------------------------
# Vehicles slot — full structured pattern roundtrip
# ---------------------------------------------------------------------------

def test_vehicles_slot_structured_with_dependencies():
    framework_root = Path(__file__).resolve().parent.parent.parent
    bank = load_questions(framework_root / "templates" / "persona-interview" / "questions.yaml")
    v = bank.find("characterbible.vehicles")
    assert v is not None
    assert v.kind == "structured"
    assert v.composition is not None
    assert v.composition.when == Dependency("owns", "==", "yes")
    assert v.composition.fallback is not None
    deps = [p for p in v.prompts if p.depends_on]
    assert len(deps) >= 8  # most prompts gated on owns == yes
