"""Loader and types for the persona-interview question bank.

Reads `templates/persona-interview/questions.yaml` and returns typed
`Slot` / `Prompt` / `Composition` dataclasses with full validation:

  - Slot ids unique.
  - Required fields present.
  - Prompt kinds in the allowed set.
  - `choice` prompts have non-empty `choices`.
  - `depends_on` references other prompts in the same slot, with a parseable
    `<id> <op> <value>` syntax.
  - `composition` only on `kind: structured` slots.
  - `composition.template` placeholders ({{<id>}}) reference prompts in the
    same slot.

Validation failures raise `QuestionsBankError` with a path to the offending
slot. The loader is consumed by Phase 5's interview engine; surfacing
errors at parse time keeps the engine's runtime simple.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None


PROMPT_KINDS = ("text", "longtext", "choice", "list")
SLOT_KINDS = ("text", "longtext", "choice", "list", "structured")
SLOT_STATUSES = ("exemplar", "draft")

# depends_on:  "<prompt_id> <op> <value>", e.g. "owns == yes"
_DEPENDS_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(==|!=)\s*(\S.*?)\s*$")

# composition placeholders:  {{<prompt_id>}}
_COMPOSITION_PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


class QuestionsBankError(ValueError):
    """Raised when the questions.yaml file is malformed or inconsistent."""


@dataclass(frozen=True)
class Validation:
    required: bool = False
    min_chars: int | None = None
    max_chars: int | None = None
    pattern: str | None = None  # regex applied at interview-answer time


@dataclass(frozen=True)
class Dependency:
    """Parsed `depends_on` clause."""
    prompt_id: str
    op: str        # == | !=
    value: str


@dataclass(frozen=True)
class Prompt:
    id: str
    text: str
    kind: str                     # text | longtext | choice | list
    choices: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    validation: Validation = field(default_factory=Validation)
    depends_on: Dependency | None = None
    help: str | None = None


@dataclass(frozen=True)
class Composition:
    template: str
    when: Dependency | None = None     # condition to use this template
    fallback: str | None = None        # used when `when` is False


@dataclass(frozen=True)
class Slot:
    slot_id: str
    target_file: str
    target_section: str
    placeholder: str
    applicability: tuple[str, ...]
    kind: str                          # text | longtext | choice | list | structured
    status: str                        # exemplar | draft
    prompts: tuple[Prompt, ...]
    composition: Composition | None = None


@dataclass(frozen=True)
class QuestionsBank:
    version: int
    slots: tuple[Slot, ...]

    def find(self, slot_id: str) -> Slot | None:
        for s in self.slots:
            if s.slot_id == slot_id:
                return s
        return None

    def for_file(self, target_file: str) -> tuple[Slot, ...]:
        return tuple(s for s in self.slots if s.target_file == target_file)


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------

def load_questions(path: Path) -> QuestionsBank:
    """Read questions.yaml, return a fully validated QuestionsBank."""
    if yaml is None:
        raise ImportError("PyYAML required: pip install pyyaml")
    if not path.exists():
        raise QuestionsBankError(f"questions.yaml not found at {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise QuestionsBankError("questions.yaml top-level must be a mapping")

    version = raw.get("version")
    if not isinstance(version, int):
        raise QuestionsBankError("missing or non-int 'version' at top-level")

    raw_slots = raw.get("slots")
    if not isinstance(raw_slots, list) or not raw_slots:
        raise QuestionsBankError("'slots' must be a non-empty list")

    seen_ids: set[str] = set()
    slots: list[Slot] = []
    for i, raw_slot in enumerate(raw_slots):
        slot = _parse_slot(raw_slot, i)
        if slot.slot_id in seen_ids:
            raise QuestionsBankError(
                f"duplicate slot_id {slot.slot_id!r} (slot index {i})"
            )
        seen_ids.add(slot.slot_id)
        slots.append(slot)

    return QuestionsBank(version=version, slots=tuple(slots))


# ---------------------------------------------------------------------------
# Slot / Prompt parsing
# ---------------------------------------------------------------------------

def _parse_slot(raw: dict, idx: int) -> Slot:
    if not isinstance(raw, dict):
        raise QuestionsBankError(f"slot at index {idx} is not a mapping")

    def need(field: str) -> object:
        if field not in raw:
            raise QuestionsBankError(
                f"slot at index {idx} missing required field {field!r}"
            )
        return raw[field]

    slot_id = need("slot_id")
    if not isinstance(slot_id, str) or not slot_id:
        raise QuestionsBankError(f"slot at index {idx}: bad slot_id {slot_id!r}")

    target_file = _str_field(raw, "target_file", slot_id)
    target_section = _str_field(raw, "target_section", slot_id)
    placeholder = _str_field(raw, "placeholder", slot_id)

    applicability = raw.get("applicability") or []
    if not isinstance(applicability, list) or not all(isinstance(a, str) for a in applicability):
        raise QuestionsBankError(f"slot {slot_id}: 'applicability' must be a list of strings")

    kind = _str_field(raw, "kind", slot_id)
    if kind not in SLOT_KINDS:
        raise QuestionsBankError(
            f"slot {slot_id}: kind {kind!r} not in {SLOT_KINDS}"
        )

    status = raw.get("status", "draft")
    if status not in SLOT_STATUSES:
        raise QuestionsBankError(
            f"slot {slot_id}: status {status!r} not in {SLOT_STATUSES}"
        )

    raw_prompts = raw.get("prompts")
    if not isinstance(raw_prompts, list) or not raw_prompts:
        raise QuestionsBankError(f"slot {slot_id}: 'prompts' must be a non-empty list")

    prompts: list[Prompt] = []
    seen_prompt_ids: set[str] = set()
    for j, raw_prompt in enumerate(raw_prompts):
        prompt = _parse_prompt(raw_prompt, slot_id, j, default_kind=kind)
        if prompt.id in seen_prompt_ids:
            raise QuestionsBankError(
                f"slot {slot_id}: duplicate prompt id {prompt.id!r}"
            )
        seen_prompt_ids.add(prompt.id)
        prompts.append(prompt)

    # Cross-prompt validation: depends_on must reference earlier prompt ids.
    for prompt in prompts:
        if prompt.depends_on is None:
            continue
        if prompt.depends_on.prompt_id not in seen_prompt_ids:
            raise QuestionsBankError(
                f"slot {slot_id}: prompt {prompt.id!r} depends_on unknown id "
                f"{prompt.depends_on.prompt_id!r}"
            )

    composition = None
    raw_composition = raw.get("composition")
    if raw_composition is not None:
        if kind != "structured":
            raise QuestionsBankError(
                f"slot {slot_id}: 'composition' is only valid on kind=structured"
            )
        composition = _parse_composition(raw_composition, slot_id, seen_prompt_ids)

    return Slot(
        slot_id=slot_id,
        target_file=target_file,
        target_section=target_section,
        placeholder=placeholder,
        applicability=tuple(applicability),
        kind=kind,
        status=status,
        prompts=tuple(prompts),
        composition=composition,
    )


def _parse_prompt(raw: dict, slot_id: str, idx: int, *, default_kind: str) -> Prompt:
    if not isinstance(raw, dict):
        raise QuestionsBankError(
            f"slot {slot_id}: prompt at index {idx} is not a mapping"
        )

    pid = raw.get("id")
    if not isinstance(pid, str) or not pid:
        raise QuestionsBankError(
            f"slot {slot_id}: prompt at index {idx} missing 'id'"
        )

    text = raw.get("text")
    if not isinstance(text, str) or not text:
        raise QuestionsBankError(
            f"slot {slot_id} prompt {pid}: missing 'text'"
        )

    kind = raw.get("kind", default_kind if default_kind in PROMPT_KINDS else "text")
    if kind not in PROMPT_KINDS:
        raise QuestionsBankError(
            f"slot {slot_id} prompt {pid}: kind {kind!r} not in {PROMPT_KINDS}"
        )

    raw_choices = raw.get("choices") or []
    if kind == "choice" and not raw_choices:
        raise QuestionsBankError(
            f"slot {slot_id} prompt {pid}: kind=choice requires non-empty 'choices'"
        )
    if not isinstance(raw_choices, list):
        raise QuestionsBankError(
            f"slot {slot_id} prompt {pid}: 'choices' must be a list"
        )
    choices = tuple(str(c) for c in raw_choices)

    raw_examples = raw.get("examples") or []
    if not isinstance(raw_examples, list):
        raise QuestionsBankError(
            f"slot {slot_id} prompt {pid}: 'examples' must be a list"
        )
    examples = tuple(str(e) for e in raw_examples)

    validation = _parse_validation(raw.get("validation") or {}, slot_id, pid)

    depends_on = None
    raw_depends = raw.get("depends_on")
    if raw_depends is not None:
        if not isinstance(raw_depends, str):
            raise QuestionsBankError(
                f"slot {slot_id} prompt {pid}: 'depends_on' must be a string"
            )
        depends_on = _parse_dependency(raw_depends, slot_id, pid)

    help_text = raw.get("help")
    if help_text is not None and not isinstance(help_text, str):
        raise QuestionsBankError(
            f"slot {slot_id} prompt {pid}: 'help' must be a string"
        )

    return Prompt(
        id=pid,
        text=text,
        kind=kind,
        choices=choices,
        examples=examples,
        validation=validation,
        depends_on=depends_on,
        help=help_text,
    )


def _parse_validation(raw: dict, slot_id: str, prompt_id: str) -> Validation:
    if not isinstance(raw, dict):
        raise QuestionsBankError(
            f"slot {slot_id} prompt {prompt_id}: 'validation' must be a mapping"
        )
    required = bool(raw.get("required", False))
    min_chars = raw.get("min_chars")
    max_chars = raw.get("max_chars")
    pattern = raw.get("pattern")
    if min_chars is not None and not isinstance(min_chars, int):
        raise QuestionsBankError(
            f"slot {slot_id} prompt {prompt_id}: min_chars must be int"
        )
    if max_chars is not None and not isinstance(max_chars, int):
        raise QuestionsBankError(
            f"slot {slot_id} prompt {prompt_id}: max_chars must be int"
        )
    if pattern is not None and not isinstance(pattern, str):
        raise QuestionsBankError(
            f"slot {slot_id} prompt {prompt_id}: pattern must be string"
        )
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.error as e:
            raise QuestionsBankError(
                f"slot {slot_id} prompt {prompt_id}: pattern is not valid regex: {e}"
            ) from e
    return Validation(
        required=required,
        min_chars=min_chars,
        max_chars=max_chars,
        pattern=pattern,
    )


def _parse_dependency(raw: str, slot_id: str, prompt_id: str) -> Dependency:
    m = _DEPENDS_RE.match(raw)
    if not m:
        raise QuestionsBankError(
            f"slot {slot_id} prompt {prompt_id}: 'depends_on' "
            f"{raw!r} does not match '<id> (==|!=) <value>'"
        )
    return Dependency(
        prompt_id=m.group(1),
        op=m.group(2),
        value=m.group(3),
    )


def _parse_composition(raw: dict, slot_id: str, prompt_ids: set[str]) -> Composition:
    if not isinstance(raw, dict):
        raise QuestionsBankError(
            f"slot {slot_id}: 'composition' must be a mapping"
        )
    template = raw.get("template")
    if not isinstance(template, str) or not template:
        raise QuestionsBankError(
            f"slot {slot_id}: composition.template must be a non-empty string"
        )

    # Validate template placeholders reference real prompt ids.
    referenced = set(_COMPOSITION_PLACEHOLDER_RE.findall(template))
    unknown = referenced - prompt_ids
    if unknown:
        raise QuestionsBankError(
            f"slot {slot_id}: composition.template references unknown prompt ids: "
            f"{sorted(unknown)}"
        )

    when = None
    raw_when = raw.get("when")
    if raw_when is not None:
        if not isinstance(raw_when, str):
            raise QuestionsBankError(
                f"slot {slot_id}: composition.when must be a string"
            )
        when = _parse_dependency(raw_when, slot_id, "<composition.when>")
        if when.prompt_id not in prompt_ids:
            raise QuestionsBankError(
                f"slot {slot_id}: composition.when references unknown prompt id "
                f"{when.prompt_id!r}"
            )

    fallback = raw.get("fallback")
    if fallback is not None and not isinstance(fallback, str):
        raise QuestionsBankError(
            f"slot {slot_id}: composition.fallback must be a string"
        )

    return Composition(template=template, when=when, fallback=fallback)


def _str_field(raw: dict, field: str, slot_id: str) -> str:
    val = raw.get(field)
    if not isinstance(val, str) or not val:
        raise QuestionsBankError(f"slot {slot_id}: missing or non-string {field!r}")
    return val
