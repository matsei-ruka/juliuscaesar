"""Interview orchestrator — walks gaps, prompts, validates, composes, splices.

The engine is I/O-agnostic: a `Prompter` Protocol abstracts interactive
input. The CLI (`bin/jc-persona`) plugs in a real terminal Prompter; tests
plug in a `DictPrompter` that returns canned answers from a dict.

Flow:

  1. Macro binding pass — find all unbound `{{persona.*}}` / `{{principal.*}}`
     / `{{employer.*}}` macros across the instance's L1/L2 files; ask the
     operator for values; rewrite files with bound values. One-shot.

  2. Gap detection — find slots that are missing or unfilled. By default
     skips populated slots; `include_populated=True` walks every slot.

  3. Slot interview — for each gap, walk visible prompts (respecting
     `depends_on`), validate each answer against `Validation`, compose the
     answers via `compose.compose()`, splice into the target file.

  4. Audit log — every slot interaction (filled, skipped, redone) is
     appended to `state/persona/interview/<timestamp>.jsonl`.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Protocol

import persona_macros  # type: ignore

from .compose import compose, visible_prompts
from .gaps import Gap, GapState, find_gaps
from .questions import (
    Prompt,
    QuestionsBank,
    Slot,
    Validation,
)
from .splice import splice_slot_body


@dataclass
class InterviewResult:
    macros_bound: dict[str, str]
    filled: list[str]              # slot_ids successfully filled
    skipped: list[str]              # slot_ids the operator chose to skip
    failed: list[tuple[str, str]]   # (slot_id, error message)
    audit_log_path: Path | None = None


class Prompter(Protocol):
    """I/O abstraction the engine uses to ask the operator for input."""

    def announce_phase(self, phase: str, detail: str = "") -> None: ...

    def ask_macro(self, macro_key: str, hint: str = "") -> str: ...

    def announce_slot(self, slot: Slot, gap: Gap, position: tuple[int, int]) -> None: ...

    def ask_prompt(self, prompt: Prompt, slot: Slot) -> str | None:
        """Returns the operator's answer, or None to skip the prompt (when allowed)."""

    def confirm_overwrite(self, slot: Slot, current_body: str) -> str:
        """Returns one of: keep | replace | skip. Used in --include-populated mode."""

    def show_message(self, message: str) -> None: ...


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def interview(
    instance_dir: Path,
    bank: QuestionsBank,
    prompter: Prompter,
    *,
    include_populated: bool = False,
    only_slot_id: str | None = None,
) -> InterviewResult:
    """Run the interview. Returns an InterviewResult summary.

    `only_slot_id`: when set, run only that single slot (used by
    `jc persona interview --redo <slot_id>`).
    """
    audit_path = _open_audit_log(instance_dir)
    result = InterviewResult(macros_bound={}, filled=[], skipped=[], failed=[],
                             audit_log_path=audit_path)

    # --- Phase 1: macro binding ---
    prompter.announce_phase("macros", "Binding persona/principal/employer values")
    bound = bind_macros_in_instance(instance_dir, prompter)
    result.macros_bound = bound
    _audit(audit_path, "macros_bound", {"keys": sorted(bound.keys())})

    # --- Phase 2: gap detection ---
    prompter.announce_phase("gaps", "Detecting which slots need attention")
    gaps = find_gaps(instance_dir, bank, include_populated=include_populated)
    if only_slot_id is not None:
        gaps = [g for g in gaps if g.slot.slot_id == only_slot_id]
        if not gaps:
            prompter.show_message(f"No matching slot for --redo {only_slot_id!r}.")
            return result

    if not gaps:
        prompter.show_message("No gaps to fill — every slot is populated.")
        return result

    # --- Phase 3: slot interview ---
    prompter.announce_phase("interview", f"{len(gaps)} slot(s) to walk")
    for i, gap in enumerate(gaps, 1):
        try:
            outcome = _interview_slot(
                instance_dir=instance_dir,
                gap=gap,
                prompter=prompter,
                bound_macros=bound,
                position=(i, len(gaps)),
            )
        except Exception as e:  # noqa: BLE001
            result.failed.append((gap.slot.slot_id, repr(e)))
            _audit(audit_path, "slot_failed", {"slot_id": gap.slot.slot_id, "error": repr(e)})
            continue

        if outcome == "filled":
            result.filled.append(gap.slot.slot_id)
        elif outcome == "skipped":
            result.skipped.append(gap.slot.slot_id)
        _audit(audit_path, f"slot_{outcome}", {"slot_id": gap.slot.slot_id})

    return result


# ---------------------------------------------------------------------------
# Macro binding — Phase 1 of the interview
# ---------------------------------------------------------------------------

_MACRO_REF_RE = re.compile(r"\{\{([a-zA-Z_]+(?:\.[a-zA-Z_]+)*)\}\}")

# Files the binder scans + rewrites. Keep this aligned with the synced
# template surface.
_BIND_FILES = (
    "memory/L1/RULES.md",
    "memory/L1/IDENTITY.md",
    "memory/L1/USER.md",
    "memory/L1/HOT.md",
    "memory/L1/JOURNAL.md",
    "memory/L1/CHATS.md",
    "ops/self_model.yaml",
    "CONTRIBUTING.md",
    "CLAUDE.md",
)


def bind_macros_in_instance(instance_dir: Path, prompter: Prompter) -> dict[str, str]:
    """Find unbound canonical macros in the instance, prompt for values, rewrite.

    Returns the values dict (also persists to ops/persona-macros.json so the
    binding is recoverable if the operator re-runs).
    """
    needed: set[str] = set()
    paths_with_macros: list[Path] = []
    for rel in _BIND_FILES:
        p = instance_dir / rel
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        found = {
            m.group(1) for m in _MACRO_REF_RE.finditer(text)
            if m.group(1) in persona_macros.CANONICAL_MACROS
        }
        if found:
            paths_with_macros.append(p)
            needed.update(found)

    # Also walk character-bible/ and cv/ subdirectories.
    for subdir in ("memory/L2/character-bible", "memory/L2/cv"):
        d = instance_dir / subdir
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            text = p.read_text(encoding="utf-8")
            found = {
                m.group(1) for m in _MACRO_REF_RE.finditer(text)
                if m.group(1) in persona_macros.CANONICAL_MACROS
            }
            if found:
                paths_with_macros.append(p)
                needed.update(found)

    # Load existing bindings (recover from prior partial runs).
    existing = _load_existing_bindings(instance_dir)
    needed -= set(existing.keys())

    values: dict[str, str] = dict(existing)
    for key in _stable_macro_order(needed):
        prompt_text = _macro_prompt_text(key)
        ans = prompter.ask_macro(key, prompt_text).strip()
        values[key] = ans

    if not values:
        return {}

    # Rewrite each file with bind_macros.
    for p in paths_with_macros:
        text = p.read_text(encoding="utf-8")
        try:
            new_text = persona_macros.bind_macros(text, values)
        except persona_macros.MacroBindingError:
            # Some macros still unbound (operator declined to provide). Leave
            # those untouched; rewrite only what we can.
            new_text = _bind_partial(text, values)
        if new_text != text:
            p.write_text(new_text, encoding="utf-8")

    _save_bindings(instance_dir, values)
    return values


def _bind_partial(text: str, values: Mapping[str, str]) -> str:
    """Substitute only the macros present in `values`; leave others as-is."""
    def replace(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in values:
            return values[key]
        return m.group(0)
    return _MACRO_REF_RE.sub(replace, text)


def _macro_prompt_text(key: str) -> str:
    hints = {
        "persona.full_name":           "Persona's full name (e.g. 'Alice Chen')",
        "persona.name":                "Persona's first name / short name (e.g. 'Alice')",
        "persona.slug":                "Persona slug for file paths (kebab-case, e.g. 'alice-chen')",
        "persona.email":               "Persona's email address",
        "persona.role":                "Persona's role title (e.g. 'Research Director')",
        "persona.role_with_employer":  "Persona's role + employer phrase (e.g. 'Research Director at MIT Media Lab')",
        "principal.full_name":         "Principal's full name",
        "principal.name":              "Principal's first name / short name",
        "principal.email":             "Principal's email address",
        "employer.full_name":          "Employer's full legal name (e.g. 'MIT Media Lab')",
        "employer.name":               "Employer's short name (e.g. 'MIT Media Lab')",
    }
    return hints.get(key, f"Value for {{{{{key}}}}}")


def _stable_macro_order(needed: set[str]) -> list[str]:
    """Order: persona first, then principal, then employer; alphabetical within each."""
    order = list(persona_macros.CANONICAL_MACROS)
    return [m for m in order if m in needed]


def _load_existing_bindings(instance_dir: Path) -> dict[str, str]:
    p = instance_dir / "ops" / "persona-macros.json"
    if not p.exists():
        return {}
    try:
        return {k: str(v) for k, v in json.loads(p.read_text(encoding="utf-8")).items()}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_bindings(instance_dir: Path, values: Mapping[str, str]) -> None:
    p = instance_dir / "ops" / "persona-macros.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(values), indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# Slot interview — Phase 3
# ---------------------------------------------------------------------------

def _interview_slot(
    *,
    instance_dir: Path,
    gap: Gap,
    prompter: Prompter,
    bound_macros: Mapping[str, str],
    position: tuple[int, int],
) -> str:
    """Return outcome: 'filled' | 'skipped'."""
    slot = gap.slot
    prompter.announce_slot(slot, gap, position)

    # Brownfield prompt for populated slots (only reached when --include-populated).
    if gap.state == GapState.POPULATED:
        decision = prompter.confirm_overwrite(slot, gap.section_body)
        if decision == "keep":
            return "skipped"
        if decision == "skip":
            return "skipped"
        # decision == "replace": fall through to interview.

    answers: dict[str, str] = {}
    for prompt in slot.prompts:
        # depends_on: skip prompts whose dependency isn't satisfied.
        if not _prompt_visible(prompt, answers):
            continue
        rendered = _render_prompt_with_macros(prompt, bound_macros)
        ans = prompter.ask_prompt(rendered, slot)
        if ans is None:
            if prompt.validation.required:
                raise InterviewSkipRequired(prompt.id)
            continue
        valid_err = _validate(ans, prompt.validation)
        if valid_err:
            prompter.show_message(f"  [validation] {valid_err}")
            # Re-ask once.
            ans = prompter.ask_prompt(rendered, slot) or ""
            valid_err = _validate(ans, prompt.validation)
            if valid_err:
                raise InterviewValidationFailure(prompt.id, valid_err)
        answers[prompt.id] = ans

    composed = compose(slot, answers)
    splice_slot_body(instance_dir, slot, composed)
    return "filled"


class InterviewSkipRequired(RuntimeError):
    """Operator skipped a required prompt."""

    def __init__(self, prompt_id: str):
        super().__init__(f"required prompt {prompt_id!r} skipped")


class InterviewValidationFailure(RuntimeError):
    """Operator's answer failed validation twice."""

    def __init__(self, prompt_id: str, message: str):
        super().__init__(f"validation failed for {prompt_id!r}: {message}")


def _prompt_visible(prompt: Prompt, answers: Mapping[str, str]) -> bool:
    if prompt.depends_on is None:
        return True
    actual = (answers.get(prompt.depends_on.prompt_id) or "").strip()
    expected = prompt.depends_on.value.strip()
    if prompt.depends_on.op == "==":
        return actual == expected
    if prompt.depends_on.op == "!=":
        return actual != expected
    return False


def _render_prompt_with_macros(prompt: Prompt, bound: Mapping[str, str]) -> Prompt:
    """Substitute already-bound macros into the prompt's text + examples."""
    if not bound:
        return prompt
    new_text = _substitute_macros(prompt.text, bound)
    new_examples = tuple(_substitute_macros(e, bound) for e in prompt.examples)
    if new_text == prompt.text and new_examples == prompt.examples:
        return prompt
    # Reconstruct as a frozen-ish replacement (Prompt is frozen; build a new one).
    return Prompt(
        id=prompt.id,
        text=new_text,
        kind=prompt.kind,
        choices=prompt.choices,
        examples=new_examples,
        validation=prompt.validation,
        depends_on=prompt.depends_on,
        help=prompt.help,
    )


def _substitute_macros(text: str, bound: Mapping[str, str]) -> str:
    def replace(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in bound:
            return bound[key]
        return m.group(0)
    return _MACRO_REF_RE.sub(replace, text)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(answer: str, v: Validation) -> str | None:
    s = answer.strip()
    if v.required and not s:
        return "answer is required"
    if v.min_chars is not None and len(s) < v.min_chars:
        return f"answer must be ≥ {v.min_chars} characters"
    if v.max_chars is not None and len(s) > v.max_chars:
        return f"answer must be ≤ {v.max_chars} characters"
    if v.pattern is not None:
        if not re.fullmatch(v.pattern, s):
            return f"answer does not match required pattern"
    return None


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _open_audit_log(instance_dir: Path) -> Path:
    log_dir = instance_dir / "state" / "persona" / "interview"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = log_dir / f"{ts}.jsonl"
    return path


def _audit(path: Path, event: str, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "event": event,
            **payload,
        }) + "\n")
