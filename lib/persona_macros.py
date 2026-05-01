"""Persona macro substitution — the bidirectional translator between source
proper nouns and `{{macro.path}}` placeholders.

Two operations:

  * apply_substitutions(text, macros)
        Sync direction. Replace every occurrence of a source string with its
        canonical {{macro}} placeholder. Used when the sync script ports a
        doctrine section verbatim into the framework template — proper nouns
        ("Mario Leone", "Filippo", "Omnisage") become macros so the template
        is portable across instances.

  * bind_macros(text, values)
        Scaffold direction. Replace every {{macro}} placeholder with a
        concrete value from a per-instance values dict. Used at `jc setup`
        time to produce coherent doctrine text in the new instance's own
        names and language.

The vocabulary of macros is fixed — see `templates/persona-interview/
macros-from-reference.yaml` for the contract. Tests in
`tests/persona/test_persona_macros.py` lock the round-trip property:

    bind_macros(apply_substitutions(text, macros), reverse_values(macros)) == text

(when `reverse_values` reconstructs the source values).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None


# ---------------------------------------------------------------------------
# Macro contract — the canonical vocabulary every reference and every
# downstream instance must agree on. Keep in sync with the doc-block in
# templates/persona-interview/macros-from-reference.yaml.
# ---------------------------------------------------------------------------

CANONICAL_MACROS: tuple[str, ...] = (
    "persona.full_name",
    "persona.name",
    "persona.slug",
    "persona.email",
    "persona.role",
    "persona.role_with_employer",
    "principal.full_name",
    "principal.name",
    "principal.email",
    "employer.full_name",
    "employer.name",
)


@dataclass(frozen=True)
class Substitution:
    """One source-string → {{macro}} mapping."""
    source: str
    macro: str
    note: str | None = None


def load_substitutions(path: Path) -> list[Substitution]:
    """Load and validate the macros-from-reference.yaml file.

    Returns substitutions sorted by source length descending — required to
    avoid prefix collisions during apply_substitutions ('Mario Leone' must
    match before 'Mario').
    """
    if yaml is None:
        raise ImportError("PyYAML required: pip install pyyaml")
    if not path.exists():
        return []

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("substitutions") or []

    subs: list[Substitution] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        macro = entry.get("macro")
        if not source or not macro:
            continue
        macro_inner = _strip_macro_braces(macro)
        if macro_inner not in CANONICAL_MACROS:
            raise ValueError(
                f"unknown macro {macro!r}; expected one of "
                f"{{{{<key>}}}} where key in CANONICAL_MACROS"
            )
        subs.append(Substitution(
            source=source,
            macro=macro,
            note=entry.get("note"),
        ))

    subs.sort(key=lambda s: len(s.source), reverse=True)
    return subs


def apply_substitutions(text: str, subs: Iterable[Substitution]) -> str:
    """Replace each substitution's source string with its {{macro}} placeholder.

    Caller is responsible for passing only doctrine-section text — applying
    substitutions to operator-authored content (REVIEWABLE/OPEN slots) would
    mangle their values.

    Substitutions are applied in the order given. `load_substitutions` already
    sorts descending by source length, so the typical usage is collision-safe.
    """
    out = text
    for s in subs:
        out = out.replace(s.source, s.macro)
    return out


def bind_macros(text: str, values: dict[str, str]) -> str:
    """Replace every {{macro.path}} occurrence with the corresponding value.

    `values` is a flat dict keyed by canonical macro names without braces:

        {"persona.full_name": "Alice Chen", "principal.name": "Sam", ...}

    Unknown macros (keys not in CANONICAL_MACROS) raise KeyError. Missing
    macros (a known key absent from `values`) raise a MacroBindingError so
    callers can detect incomplete bindings before scaffolding.
    """
    unknown = set(values.keys()) - set(CANONICAL_MACROS)
    if unknown:
        raise KeyError(f"unknown macro keys: {sorted(unknown)}")

    pattern = re.compile(r"\{\{([a-zA-Z_]+(?:\.[a-zA-Z_]+)*)\}\}")
    missing: set[str] = set()

    def replace(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in CANONICAL_MACROS:
            return m.group(0)
        if key not in values:
            missing.add(key)
            return m.group(0)
        return values[key]

    out = pattern.sub(replace, text)

    if missing:
        raise MacroBindingError(
            f"text references macros that are not bound: {sorted(missing)}"
        )
    return out


def find_unbound_macros(text: str) -> list[str]:
    """Return canonical macro names referenced in `text` (sorted, deduped)."""
    pattern = re.compile(r"\{\{([a-zA-Z_]+(?:\.[a-zA-Z_]+)*)\}\}")
    found = {m.group(1) for m in pattern.finditer(text) if m.group(1) in CANONICAL_MACROS}
    return sorted(found)


class MacroBindingError(RuntimeError):
    """Raised by bind_macros when text references a macro not in `values`."""


def _strip_macro_braces(macro: str) -> str:
    """'{{persona.full_name}}' -> 'persona.full_name'."""
    s = macro.strip()
    if s.startswith("{{") and s.endswith("}}"):
        return s[2:-2]
    return s
