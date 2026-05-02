"""Tests for lib/persona_macros.py — the macro substitution and binding."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make lib/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

import persona_macros as pm  # noqa: E402


# ---------------------------------------------------------------------------
# load_substitutions
# ---------------------------------------------------------------------------

def test_load_substitutions_sorts_descending(tmp_path: Path):
    """Length-descending order is required to avoid prefix collisions."""
    f = tmp_path / "macros.yaml"
    f.write_text("""
substitutions:
  - source: "Mario"
    macro: "{{persona.name}}"
  - source: "Mario Leone"
    macro: "{{persona.full_name}}"
  - source: "Omnisage"
    macro: "{{employer.name}}"
""")
    subs = pm.load_substitutions(f)
    sources = [s.source for s in subs]
    assert sources == ["Mario Leone", "Omnisage", "Mario"]


def test_load_substitutions_rejects_unknown_macros(tmp_path: Path):
    f = tmp_path / "macros.yaml"
    f.write_text("""
substitutions:
  - source: "X"
    macro: "{{not.a.real.macro}}"
""")
    with pytest.raises(ValueError, match="unknown macro"):
        pm.load_substitutions(f)


def test_load_substitutions_missing_file_returns_empty(tmp_path: Path):
    assert pm.load_substitutions(tmp_path / "missing.yaml") == []


# ---------------------------------------------------------------------------
# apply_substitutions (sync direction: source → macros)
# ---------------------------------------------------------------------------

def test_apply_substitutions_replaces_proper_nouns():
    subs = [
        pm.Substitution("Mario Leone", "{{persona.full_name}}"),
        pm.Substitution("Mario", "{{persona.name}}"),
        pm.Substitution("Filippo Perta", "{{principal.full_name}}"),
        pm.Substitution("Filippo", "{{principal.name}}"),
        pm.Substitution("Omnisage LLC", "{{employer.full_name}}"),
        pm.Substitution("Omnisage", "{{employer.name}}"),
    ]
    # Sort like load_substitutions does.
    subs.sort(key=lambda s: len(s.source), reverse=True)

    text = (
        "Mario Leone è un esperimento di Filippo Perta presso Omnisage LLC.\n"
        "Mario lavora come COO. Filippo supervisiona. Omnisage paga le bollette.\n"
    )
    out = pm.apply_substitutions(text, subs)

    assert "{{persona.full_name}} è un esperimento" in out
    assert "di {{principal.full_name}}" in out
    assert "presso {{employer.full_name}}" in out
    assert "{{persona.name}} lavora come" in out
    assert "{{principal.name}} supervisiona" in out
    assert "{{employer.name}} paga" in out
    # No raw proper nouns left.
    assert "Mario" not in out
    assert "Filippo" not in out
    assert "Omnisage" not in out


def test_apply_substitutions_prefix_collision_safe_with_proper_order():
    """'Mario' must NOT match before 'Mario Leone' — descending length sort handles this."""
    subs = sorted(
        [
            pm.Substitution("Mario", "{{persona.name}}"),
            pm.Substitution("Mario Leone", "{{persona.full_name}}"),
        ],
        key=lambda s: len(s.source),
        reverse=True,
    )
    text = "Mario Leone is here, Mario alone is also here."
    out = pm.apply_substitutions(text, subs)
    assert out == "{{persona.full_name}} is here, {{persona.name}} alone is also here."


# ---------------------------------------------------------------------------
# bind_macros (scaffold direction: macros → values)
# ---------------------------------------------------------------------------

def test_bind_macros_substitutes_known_keys():
    text = "{{persona.full_name}} works for {{employer.name}}."
    out = pm.bind_macros(text, {
        "persona.full_name": "Alice Chen",
        "employer.name": "MIT Media Lab",
    })
    assert out == "Alice Chen works for MIT Media Lab."


def test_bind_macros_rejects_unknown_keys():
    with pytest.raises(KeyError, match="unknown macro keys"):
        pm.bind_macros("Hello", {"not.real": "X"})


def test_bind_macros_raises_on_unbound_referenced_macros():
    text = "{{persona.full_name}} and {{principal.name}}"
    with pytest.raises(pm.MacroBindingError, match="not bound"):
        pm.bind_macros(text, {"persona.full_name": "Alice"})


def test_bind_macros_leaves_unknown_macro_braces_alone():
    """Macros with non-canonical paths (typos, future macros) pass through untouched."""
    text = "{{persona.name}} and {{some.weird.macro}}"
    out = pm.bind_macros(text, {"persona.name": "Mario"})
    assert "Mario" in out
    assert "{{some.weird.macro}}" in out


# ---------------------------------------------------------------------------
# Round-trip property
# ---------------------------------------------------------------------------

def test_round_trip_apply_then_bind():
    """apply_substitutions(text) followed by bind_macros(values) == original text."""
    subs = sorted(
        [
            pm.Substitution("Mario Leone", "{{persona.full_name}}"),
            pm.Substitution("Mario", "{{persona.name}}"),
            pm.Substitution("Filippo", "{{principal.name}}"),
            pm.Substitution("Omnisage", "{{employer.name}}"),
        ],
        key=lambda s: len(s.source),
        reverse=True,
    )
    original = "Mario Leone (called Mario) reports to Filippo at Omnisage."
    placeholdered = pm.apply_substitutions(original, subs)
    rebound = pm.bind_macros(placeholdered, {
        "persona.full_name": "Mario Leone",
        "persona.name": "Mario",
        "principal.name": "Filippo",
        "employer.name": "Omnisage",
    })
    assert rebound == original


def test_round_trip_with_different_persona():
    """The placeholderized template binds cleanly to a totally different persona."""
    subs = sorted(
        [
            pm.Substitution("Mario Leone", "{{persona.full_name}}"),
            pm.Substitution("Mario", "{{persona.name}}"),
        ],
        key=lambda s: len(s.source),
        reverse=True,
    )
    template = pm.apply_substitutions(
        "Mario Leone è un esperimento. Mario lavora come COO.", subs,
    )
    bound = pm.bind_macros(template, {
        "persona.full_name": "Alice Chen",
        "persona.name": "Alice",
    })
    assert bound == "Alice Chen è un esperimento. Alice lavora come COO."


# ---------------------------------------------------------------------------
# find_unbound_macros
# ---------------------------------------------------------------------------

def test_find_unbound_macros_lists_canonical_only():
    text = """
    {{persona.full_name}} and {{principal.email}} but also {{some.unknown}}
    plus {{persona.full_name}} again (dedup).
    """
    found = pm.find_unbound_macros(text)
    assert found == ["persona.full_name", "principal.email"]


# ---------------------------------------------------------------------------
# Real reference macros file
# ---------------------------------------------------------------------------

def test_reference_macros_file_loads(tmp_path: Path):
    """The shipped templates/persona-interview/macros-from-reference.yaml is valid."""
    framework_root = Path(__file__).resolve().parent.parent.parent
    path = framework_root / "templates" / "persona-interview" / "macros-from-reference.yaml"
    assert path.exists(), "shipped macros-from-reference.yaml is missing"
    subs = pm.load_substitutions(path)
    assert len(subs) > 0
    # Every substitution macro should be in the canonical vocabulary.
    for s in subs:
        inner = pm._strip_macro_braces(s.macro)
        assert inner in pm.CANONICAL_MACROS, f"non-canonical macro: {s.macro}"
