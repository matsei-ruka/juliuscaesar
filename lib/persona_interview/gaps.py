"""Gap detection — find slots that need to be filled.

Given an instance directory + a QuestionsBank, classify each slot as one of:

  - missing    — the target file does not exist, or the target section is
                 absent inside an existing file.
  - unfilled   — the slot's `{{slot:<id>}}` placeholder is still present in
                 the section body. (Heuristics also catch sections whose body
                 is empty/whitespace-only or contains only the ASK hint.)
  - populated  — the section is present and has real content beyond the
                 placeholder/ASK hint.

The classifier is intentionally conservative: anything ambiguous defaults
to `populated` so the engine doesn't accidentally overwrite operator content
in gap-fill mode.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .questions import QuestionsBank, Slot


class GapState(str, Enum):
    MISSING = "missing"
    UNFILLED = "unfilled"
    POPULATED = "populated"


@dataclass(frozen=True)
class Gap:
    """A slot's status against the current instance contents."""
    slot: Slot
    state: GapState
    target_path: Path           # absolute path to the target file (macro-resolved)
    section_body: str           # body of the section as currently in the file
                                # (empty if missing, may contain placeholder if unfilled)


_SECTION_HEADING_RE = re.compile(r"^## .+$", re.MULTILINE)
_ASK_COMMENT_RE = re.compile(r"<!--\s*ASK:.*?-->", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_PATH_PLACEHOLDER_RE = re.compile(r"<([a-zA-Z_][a-zA-Z0-9_.]*)>")


def _load_macros(instance_dir: Path) -> dict[str, str]:
    """Read ops/persona-macros.json. Empty dict on missing/malformed file."""
    p = instance_dir / "ops" / "persona-macros.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: str(v) for k, v in raw.items() if isinstance(k, str) and v}


def _resolve_target_path(
    instance_dir: Path,
    target_file: str,
    macros: dict[str, str],
) -> tuple[Path, bool]:
    """Resolve `<key>` placeholders in target_file via macro bindings.

    Lookup order for `<foo>`: `persona.foo` → bare `foo`. The slug case
    (`<slug>` → `persona.slug`) is the motivating use; the generic form
    keeps the resolver future-proof for other path-templated slots.

    Returns (resolved_path, fully_resolved). `fully_resolved=False` means
    one or more placeholders had no binding — caller should treat the slot
    as MISSING (operator hasn't completed the macro phase yet).
    """
    def replace(m: re.Match[str]) -> str:
        key = m.group(1)
        for candidate in (f"persona.{key}", key):
            v = macros.get(candidate)
            if v:
                return v
        return m.group(0)

    resolved = _PATH_PLACEHOLDER_RE.sub(replace, target_file)
    fully_resolved = _PATH_PLACEHOLDER_RE.search(resolved) is None
    return instance_dir / resolved, fully_resolved


def find_gaps(
    instance_dir: Path,
    bank: QuestionsBank,
    *,
    include_populated: bool = False,
) -> list[Gap]:
    """Walk the instance and classify every slot.

    Default returns only `missing` and `unfilled` slots — what the engine
    needs to fill. With `include_populated=True`, returns every slot
    regardless of state (used by `--include-populated` brownfield mode).
    """
    macros = _load_macros(instance_dir)
    out: list[Gap] = []
    for slot in bank.slots:
        gap = classify_slot(instance_dir, slot, macros=macros)
        if gap.state == GapState.POPULATED and not include_populated:
            continue
        out.append(gap)
    return out


def classify_slot(
    instance_dir: Path,
    slot: Slot,
    *,
    macros: dict[str, str] | None = None,
) -> Gap:
    """Determine the state of one slot.

    `macros` is the loaded `ops/persona-macros.json` mapping; when omitted
    we lazy-load so single-slot callers (tests, `--redo`) still resolve
    macro-templated paths like `memory/L2/character-bible/<slug>.md`.
    """
    if macros is None:
        macros = _load_macros(instance_dir)
    target_path, fully_resolved = _resolve_target_path(
        instance_dir, slot.target_file, macros
    )
    if not fully_resolved or not target_path.exists():
        return Gap(slot=slot, state=GapState.MISSING, target_path=target_path, section_body="")

    text = target_path.read_text(encoding="utf-8")
    section_body = _extract_section_body(text, slot.target_section)

    if section_body is None:
        return Gap(slot=slot, state=GapState.MISSING, target_path=target_path, section_body="")

    placeholder = slot.placeholder
    if placeholder in section_body:
        return Gap(slot=slot, state=GapState.UNFILLED, target_path=target_path, section_body=section_body)

    # Heuristic: section body, with markers + comments stripped, is empty or
    # only contains a TODO marker → treat as unfilled.
    cleaned = _strip_markers_and_comments(section_body).strip()
    if not cleaned or cleaned in {"TODO", "todo", "-", "_", "(empty)"}:
        return Gap(slot=slot, state=GapState.UNFILLED, target_path=target_path, section_body=section_body)

    return Gap(slot=slot, state=GapState.POPULATED, target_path=target_path, section_body=section_body)


def _extract_section_body(text: str, target_heading: str) -> str | None:
    """Return the body under `target_heading` (until the next ## or EOF), or None.

    Match is exact on the heading line. The body excludes the heading itself
    but includes everything until the next H2 heading or end of file.
    """
    pattern = re.compile(
        r"^" + re.escape(target_heading) + r"\s*\n",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        return None
    start = match.end()
    next_heading = _SECTION_HEADING_RE.search(text, pos=start)
    end = next_heading.start() if next_heading else len(text)
    return text[start:end]


def _strip_markers_and_comments(body: str) -> str:
    """Remove HTML comments (markers + ASK hints) so we can detect 'effectively empty'."""
    return _HTML_COMMENT_RE.sub("", body)


def summarize(gaps: list[Gap]) -> dict:
    """Counts by state, useful for `jc persona gaps --json`."""
    summary: dict[str, int] = {s.value: 0 for s in GapState}
    for g in gaps:
        summary[g.state.value] += 1
    summary["total"] = len(gaps)
    return summary
