"""Tests for lib/persona_interview/engine.py — end-to-end with FakePrompter."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from persona_interview.engine import (  # noqa: E402
    InterviewResult,
    Prompter,
    bind_macros_in_instance,
    interview,
)
from persona_interview.gaps import Gap, GapState  # noqa: E402
from persona_interview.questions import (  # noqa: E402
    Composition,
    Dependency,
    Prompt,
    QuestionsBank,
    Slot,
    Validation,
)


# ---------------------------------------------------------------------------
# FakePrompter: deterministic, returns canned answers.
# ---------------------------------------------------------------------------

@dataclass
class FakePrompter:
    macros: dict[str, str] = field(default_factory=dict)
    answers: dict[str, dict[str, str]] = field(default_factory=dict)
    overwrite_decisions: dict[str, str] = field(default_factory=dict)
    slot_body_decisions: dict[str, list[str]] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
    phases_announced: list[str] = field(default_factory=list)
    prompt_calls: list[tuple[str, str]] = field(default_factory=list)
    previews: list[tuple[str, str]] = field(default_factory=list)

    def announce_phase(self, phase, detail=""):
        self.phases_announced.append(f"{phase}:{detail}")

    def announce_slot(self, slot, gap, position):
        pass

    def ask_macro(self, macro_key, hint=""):
        return self.macros.get(macro_key, "")

    def ask_prompt(self, prompt, slot):
        self.prompt_calls.append((slot.slot_id, prompt.id))
        return self.answers.get(slot.slot_id, {}).get(prompt.id)

    def confirm_overwrite(self, slot, current_body):
        return self.overwrite_decisions.get(slot.slot_id, "skip")

    def confirm_slot_body(self, slot, composed_body):
        self.previews.append((slot.slot_id, composed_body))
        decisions = self.slot_body_decisions.get(slot.slot_id)
        if decisions:
            return decisions.pop(0)
        return "apply"

    def show_message(self, message):
        self.messages.append(message)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

def _slot(slot_id, target_file, target_section, kind="text", prompts=None,
          composition=None) -> Slot:
    return Slot(
        slot_id=slot_id,
        target_file=target_file,
        target_section=target_section,
        placeholder=f"{{{{slot:{slot_id}}}}}",
        applicability=("always",),
        kind=kind,
        status="exemplar",
        prompts=tuple(prompts or [Prompt(
            id="q", text="?", kind="text", validation=Validation(required=True),
            choices=(), examples=(), depends_on=None,
        )]),
        composition=composition,
    )


def _build_instance(tmp_path: Path, files: Mapping[str, str]) -> Path:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    (tmp_path / ".jc").write_text("", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Macro binding
# ---------------------------------------------------------------------------

def test_bind_macros_replaces_in_files(tmp_path):
    _build_instance(tmp_path, {
        "memory/L1/RULES.md": "Hello {{persona.full_name}} from {{employer.name}}.\n",
        "memory/L1/IDENTITY.md": "{{persona.name}} reports to {{principal.name}}.\n",
    })
    prompter = FakePrompter(macros={
        "persona.full_name": "Alice Chen",
        "persona.name": "Alice",
        "employer.name": "MIT Media Lab",
        "principal.name": "Sam",
    })
    bound = bind_macros_in_instance(tmp_path, prompter)
    assert bound["persona.full_name"] == "Alice Chen"

    rules = (tmp_path / "memory/L1/RULES.md").read_text(encoding="utf-8")
    identity = (tmp_path / "memory/L1/IDENTITY.md").read_text(encoding="utf-8")
    assert "Alice Chen" in rules
    assert "MIT Media Lab" in rules
    assert "Alice reports to Sam." in identity


def test_bind_macros_persists_to_ops_persona_macros_json(tmp_path):
    _build_instance(tmp_path, {
        "memory/L1/RULES.md": "Hi {{persona.name}}.\n",
    })
    bind_macros_in_instance(tmp_path, FakePrompter(macros={"persona.name": "Mario"}))
    bindings_path = tmp_path / "ops" / "persona-macros.json"
    assert bindings_path.exists()
    import json
    saved = json.loads(bindings_path.read_text(encoding="utf-8"))
    assert saved["persona.name"] == "Mario"


def test_bind_macros_recovers_from_existing_bindings(tmp_path):
    _build_instance(tmp_path, {
        "memory/L1/RULES.md": "Hi {{persona.name}}, {{employer.name}}.\n",
        "ops/persona-macros.json": '{"persona.name": "Mario"}',
    })
    prompter = FakePrompter(macros={"employer.name": "Omnisage"})
    bind_macros_in_instance(tmp_path, prompter)
    rules = (tmp_path / "memory/L1/RULES.md").read_text(encoding="utf-8")
    assert "Hi Mario, Omnisage." in rules


def test_bind_macros_no_unbound_macros_is_noop(tmp_path):
    _build_instance(tmp_path, {
        "memory/L1/RULES.md": "Plain content, no macros.\n",
    })
    prompter = FakePrompter()
    bound = bind_macros_in_instance(tmp_path, prompter)
    assert bound == {}


# ---------------------------------------------------------------------------
# End-to-end interview
# ---------------------------------------------------------------------------

def test_interview_fills_unfilled_slot(tmp_path):
    _build_instance(tmp_path, {
        "memory/L1/RULES.md": "## §2 — OPERATING MODES\n<!-- REVIEWABLE -->\n\n{{slot:rules.modes}}\n",
    })
    bank = QuestionsBank(version=1, slots=(
        _slot("rules.modes", "memory/L1/RULES.md", "## §2 — OPERATING MODES"),
    ))
    prompter = FakePrompter(answers={"rules.modes": {"q": "Three modes: Founder / Collaborative / External."}})
    result = interview(tmp_path, bank, prompter)
    assert "rules.modes" in result.filled
    assert not result.failed
    text = (tmp_path / "memory/L1/RULES.md").read_text(encoding="utf-8")
    assert "Three modes" in text
    assert "{{slot:" not in text


def test_interview_skips_populated_by_default(tmp_path):
    _build_instance(tmp_path, {
        "memory/L1/RULES.md": "## §2 — OPERATING MODES\n<!-- REVIEWABLE -->\n\nReal authored content here.\n",
    })
    bank = QuestionsBank(version=1, slots=(
        _slot("rules.modes", "memory/L1/RULES.md", "## §2 — OPERATING MODES"),
    ))
    prompter = FakePrompter(answers={"rules.modes": {"q": "would-overwrite"}})
    result = interview(tmp_path, bank, prompter)
    assert result.filled == []
    text = (tmp_path / "memory/L1/RULES.md").read_text(encoding="utf-8")
    assert "Real authored content" in text  # untouched


def test_interview_brownfield_replace_overwrites_with_backup(tmp_path):
    _build_instance(tmp_path, {
        "memory/L1/RULES.md": "## §2 — OPERATING MODES\n<!-- REVIEWABLE -->\n\nOld content.\n",
    })
    bank = QuestionsBank(version=1, slots=(
        _slot("rules.modes", "memory/L1/RULES.md", "## §2 — OPERATING MODES"),
    ))
    prompter = FakePrompter(
        answers={"rules.modes": {"q": "New content."}},
        overwrite_decisions={"rules.modes": "replace"},
    )
    result = interview(tmp_path, bank, prompter, include_populated=True)
    assert "rules.modes" in result.filled
    text = (tmp_path / "memory/L1/RULES.md").read_text(encoding="utf-8")
    assert "New content." in text
    assert "Old content." not in text
    # Backup should exist.
    backups = list((tmp_path / "state" / "persona" / "redo").rglob("*.bak"))
    assert backups
    assert "Old content." in backups[0].read_text(encoding="utf-8")


def test_interview_brownfield_keep_does_not_modify(tmp_path):
    _build_instance(tmp_path, {
        "memory/L1/RULES.md": "## §2 — OPERATING MODES\n<!-- REVIEWABLE -->\n\nOld content.\n",
    })
    bank = QuestionsBank(version=1, slots=(
        _slot("rules.modes", "memory/L1/RULES.md", "## §2 — OPERATING MODES"),
    ))
    prompter = FakePrompter(
        answers={"rules.modes": {"q": "New content."}},
        overwrite_decisions={"rules.modes": "keep"},
    )
    result = interview(tmp_path, bank, prompter, include_populated=True)
    assert "rules.modes" in result.skipped
    text = (tmp_path / "memory/L1/RULES.md").read_text(encoding="utf-8")
    assert "Old content." in text


def test_interview_multi_prompt_slot_previews_before_splice(tmp_path):
    _build_instance(tmp_path, {
        "memory/L1/IDENTITY.md": "## Character\n<!-- OPEN -->\n\n{{slot:identity.character}}\n",
    })
    slot = _slot(
        "identity.character",
        "memory/L1/IDENTITY.md",
        "## Character",
        kind="structured",
        prompts=[
            Prompt(id="bio", text="Bio?", kind="longtext", validation=Validation(required=True)),
            Prompt(id="voice", text="Voice?", kind="longtext", validation=Validation(required=False)),
        ],
        composition=Composition(template="Bio:\n{{bio}}\n\nVoice:\n{{voice}}\n"),
    )
    bank = QuestionsBank(version=1, slots=(slot,))
    prompter = FakePrompter(
        answers={"identity.character": {"bio": "Personal Details\n\nFull name: Florian Berger"}},
        slot_body_decisions={"identity.character": ["apply"]},
    )

    result = interview(tmp_path, bank, prompter)

    assert "identity.character" in result.filled
    assert prompter.previews == [
        (
            "identity.character",
            "Bio:\nPersonal Details\n\nFull name: Florian Berger\n\nVoice:\n",
        )
    ]
    text = (tmp_path / "memory/L1/IDENTITY.md").read_text(encoding="utf-8")
    assert "Personal Details\n\nFull name: Florian Berger" in text


def test_interview_multi_prompt_redo_restarts_without_intermediate_splice(tmp_path):
    _build_instance(tmp_path, {
        "memory/L1/IDENTITY.md": "## Character\n<!-- OPEN -->\n\n{{slot:identity.character}}\n",
    })
    slot = _slot(
        "identity.character",
        "memory/L1/IDENTITY.md",
        "## Character",
        kind="structured",
        prompts=[
            Prompt(id="bio", text="Bio?", kind="text", validation=Validation(required=True)),
            Prompt(id="voice", text="Voice?", kind="text", validation=Validation(required=False)),
        ],
    )
    bank = QuestionsBank(version=1, slots=(slot,))
    prompter = FakePrompter(
        answers={"identity.character": {"bio": "Bio", "voice": "Voice"}},
        slot_body_decisions={"identity.character": ["redo", "apply"]},
    )

    result = interview(tmp_path, bank, prompter)

    assert "identity.character" in result.filled
    assert len(prompter.previews) == 2
    assert prompter.prompt_calls == [
        ("identity.character", "bio"),
        ("identity.character", "voice"),
        ("identity.character", "bio"),
        ("identity.character", "voice"),
    ]
    text = (tmp_path / "memory/L1/IDENTITY.md").read_text(encoding="utf-8")
    assert text.count("- **bio:** Bio") == 1


def test_interview_multi_prompt_abort_leaves_file_unchanged_and_audits_skip(tmp_path):
    original = "## Character\n<!-- OPEN -->\n\n{{slot:identity.character}}\n"
    _build_instance(tmp_path, {"memory/L1/IDENTITY.md": original})
    slot = _slot(
        "identity.character",
        "memory/L1/IDENTITY.md",
        "## Character",
        kind="structured",
        prompts=[
            Prompt(id="bio", text="Bio?", kind="text", validation=Validation(required=True)),
            Prompt(id="voice", text="Voice?", kind="text", validation=Validation(required=False)),
        ],
    )
    bank = QuestionsBank(version=1, slots=(slot,))
    prompter = FakePrompter(
        answers={"identity.character": {"bio": "Bio", "voice": "Voice"}},
        slot_body_decisions={"identity.character": ["abort"]},
    )

    result = interview(tmp_path, bank, prompter)

    assert "identity.character" in result.skipped
    text = (tmp_path / "memory/L1/IDENTITY.md").read_text(encoding="utf-8")
    assert text == original
    assert result.audit_log_path is not None
    assert '"event": "slot_skipped"' in result.audit_log_path.read_text(encoding="utf-8")


def test_interview_only_slot_id_targets_one(tmp_path):
    _build_instance(tmp_path, {
        "memory/L1/RULES.md": (
            "## §2 — A\n<!-- OPEN -->\n\n{{slot:rules.a}}\n"
            "## §3 — B\n<!-- OPEN -->\n\n{{slot:rules.b}}\n"
        ),
    })
    bank = QuestionsBank(version=1, slots=(
        _slot("rules.a", "memory/L1/RULES.md", "## §2 — A"),
        _slot("rules.b", "memory/L1/RULES.md", "## §3 — B"),
    ))
    prompter = FakePrompter(
        answers={"rules.a": {"q": "A done."}, "rules.b": {"q": "B done."}},
        overwrite_decisions={"rules.a": "replace", "rules.b": "replace"},
    )
    result = interview(tmp_path, bank, prompter, only_slot_id="rules.a")
    assert "rules.a" in result.filled
    assert "rules.b" not in result.filled


def test_interview_writes_audit_log(tmp_path):
    _build_instance(tmp_path, {
        "memory/L1/RULES.md": "## §2 — A\n\n{{slot:rules.a}}\n",
    })
    bank = QuestionsBank(version=1, slots=(
        _slot("rules.a", "memory/L1/RULES.md", "## §2 — A"),
    ))
    prompter = FakePrompter(answers={"rules.a": {"q": "X"}})
    result = interview(tmp_path, bank, prompter)
    assert result.audit_log_path is not None
    assert result.audit_log_path.exists()
    log = result.audit_log_path.read_text(encoding="utf-8")
    assert "macros_bound" in log
    assert "slot_filled" in log
