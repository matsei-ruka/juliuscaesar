"""Compose slot answers into markdown body.

Given a `Slot` definition + a dict of `{prompt_id: answer}`, produce the
markdown that fills the section's `{{slot:<id>}}` placeholder.

Three composition paths, by slot kind:

  - structured: evaluate `composition.when` against the answers; if true
    (or absent), substitute `{{prompt_id}}` placeholders in
    `composition.template` with the corresponding answer; if false and a
    `composition.fallback` exists, use the fallback verbatim.

  - text / longtext: emit the single answer as a paragraph block.

  - choice: emit "selected_value" as a single line.

  - list: emit "- item\\n- item\\n..." for each line of the answer.

When a structured slot has no `composition.template`, fall back to a simple
"key: value\\n" block over the prompts (in declaration order, skipping any
prompts whose `depends_on` evaluates false).
"""

from __future__ import annotations

import re
from typing import Mapping

from .questions import Composition, Dependency, Prompt, Slot


_PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


def compose(slot: Slot, answers: Mapping[str, str]) -> str:
    """Render the slot's body markdown from collected answers."""
    if slot.kind == "structured":
        return _compose_structured(slot, answers)
    if slot.kind == "list":
        return _compose_list(slot, answers)
    if slot.kind == "choice":
        return _compose_choice(slot, answers)
    # text / longtext: single-prompt path.
    return _compose_single(slot, answers)


# ---------------------------------------------------------------------------
# Structured slots
# ---------------------------------------------------------------------------

def _compose_structured(slot: Slot, answers: Mapping[str, str]) -> str:
    composition = slot.composition

    if composition is not None and composition.when is not None:
        if not _eval_dependency(composition.when, answers):
            return composition.fallback or ""

    if composition is not None:
        return _render_template(composition.template, answers)

    # Default: simple key: value block over visible prompts.
    lines: list[str] = []
    for p in slot.prompts:
        if not _prompt_visible(p, answers):
            continue
        ans = (answers.get(p.id) or "").strip()
        if not ans:
            continue
        lines.append(f"- **{p.id}:** {ans}")
    return "\n".join(lines)


def _render_template(template: str, answers: Mapping[str, str]) -> str:
    """Substitute {{prompt_id}} placeholders. Missing/empty → empty string."""
    def replace(m: re.Match[str]) -> str:
        return (answers.get(m.group(1)) or "").strip()
    return _PLACEHOLDER_RE.sub(replace, template).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Single-prompt slots
# ---------------------------------------------------------------------------

def _compose_single(slot: Slot, answers: Mapping[str, str]) -> str:
    if not slot.prompts:
        return ""
    pid = slot.prompts[0].id
    ans = (answers.get(pid) or "").strip()
    return ans + "\n" if ans else ""


def _compose_list(slot: Slot, answers: Mapping[str, str]) -> str:
    if not slot.prompts:
        return ""
    pid = slot.prompts[0].id
    raw = (answers.get(pid) or "").strip()
    if not raw:
        return ""
    items = [line.strip() for line in raw.splitlines() if line.strip()]
    bulleted = []
    for item in items:
        if item.startswith("- ") or item.startswith("* "):
            bulleted.append(item)
        else:
            bulleted.append(f"- {item}")
    return "\n".join(bulleted) + "\n"


def _compose_choice(slot: Slot, answers: Mapping[str, str]) -> str:
    if not slot.prompts:
        return ""
    pid = slot.prompts[0].id
    ans = (answers.get(pid) or "").strip()
    return ans + "\n" if ans else ""


# ---------------------------------------------------------------------------
# Dependency evaluation
# ---------------------------------------------------------------------------

def _prompt_visible(prompt: Prompt, answers: Mapping[str, str]) -> bool:
    """A prompt is 'visible' (asked + composed) iff its depends_on evaluates true."""
    if prompt.depends_on is None:
        return True
    return _eval_dependency(prompt.depends_on, answers)


def _eval_dependency(dep: Dependency, answers: Mapping[str, str]) -> bool:
    actual = (answers.get(dep.prompt_id) or "").strip()
    expected = dep.value.strip()
    if dep.op == "==":
        return actual == expected
    if dep.op == "!=":
        return actual != expected
    raise ValueError(f"unknown dependency operator: {dep.op!r}")


def visible_prompts(slot: Slot, answers: Mapping[str, str]) -> tuple[Prompt, ...]:
    """Filter slot.prompts to those whose depends_on evaluates true given current answers."""
    return tuple(p for p in slot.prompts if _prompt_visible(p, answers))
