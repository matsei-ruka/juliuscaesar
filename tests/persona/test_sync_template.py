"""Tests for scripts/sync_persona_template.py.

These tests build small synthetic source documents and check the sync logic
against the spec invariants:

- Doctrine sections (in DOCTRINE_SECTIONS) marked IMMUTABILE pass through verbatim.
- Sections marked IMMUTABILE but NOT in DOCTRINE_SECTIONS are slotified.
- Sections marked REVIEWABLE / OPEN are slotified with the right marker.
- Slot id resolution prefers overrides over auto-derivation.
- find_unresolved_placeholders flags `{{slot:...}}` in the source.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make scripts/ importable for tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import sync_persona_template as sync  # noqa: E402


# ---------------------------------------------------------------------------
# Section parsing
# ---------------------------------------------------------------------------

def test_split_into_sections_basic():
    src = """---
slug: TEST
---

# Title

Some preamble.

## §0 — DOCTRINE
<!-- IMMUTABILE -->

Body of doctrine.

## §2 — SOMETHING ELSE
<!-- REVIEWABLE -->

Body of reviewable.
"""
    preamble, sections = sync.split_into_sections(src)
    assert "Some preamble" in preamble
    assert len(sections) == 2
    assert sections[0].heading == "## §0 — DOCTRINE"
    assert sections[0].marker == "IMMUTABILE"
    assert sections[1].heading == "## §2 — SOMETHING ELSE"
    assert sections[1].marker == "REVIEWABLE"


def test_marker_detection_only_first_three_lines():
    """Marker must appear within first 3 non-empty lines after the heading."""
    src_close = "## H\n<!-- IMMUTABILE -->\n\nBody.\n"
    src_far = "## H\n\nLine 1\n\nLine 2\n\nLine 3\n\n<!-- IMMUTABILE -->\n\nBody.\n"
    _, [a] = sync.split_into_sections(src_close)
    _, [b] = sync.split_into_sections(src_far)
    assert a.marker == "IMMUTABILE"
    assert b.marker is None


def test_no_marker_returns_none():
    src = "## H\n\nJust a body, no marker.\n"
    _, [s] = sync.split_into_sections(src)
    assert s.marker is None


# ---------------------------------------------------------------------------
# Doctrine annotation
# ---------------------------------------------------------------------------

def test_doctrine_annotation_matches_rules_patterns():
    """Sections matching DOCTRINE_SECTIONS for RULES.md are flagged."""
    src = """## §0 — DOTTRINA TRASPARENZA AI
<!-- IMMUTABILE -->

Body.

## §2 — TRE MODE OPERATIVI
<!-- REVIEWABLE -->

Body.

## HARD RULE — Policy authority: Filippo only
<!-- IMMUTABILE -->

Body.
"""
    _, sections = sync.split_into_sections(src)
    sync.annotate_doctrine("memory/L1/RULES.md", sections)
    assert sections[0].is_doctrine is True   # §0 is doctrine
    assert sections[1].is_doctrine is False  # §2 is REVIEWABLE, not doctrine
    assert sections[2].is_doctrine is False  # HARD RULE is IMMUTABILE but not in DOCTRINE list


# ---------------------------------------------------------------------------
# Slot id resolution
# ---------------------------------------------------------------------------

def test_resolve_slot_id_prefers_override():
    overrides = {
        "sections": {
            "memory/L1/RULES.md": {
                "## §2 — TRE MODE OPERATIVI": {"slot_id": "rules.operating-modes"},
            },
        },
    }
    sid = sync.resolve_slot_id(
        "memory/L1/RULES.md", "## §2 — TRE MODE OPERATIVI", overrides,
    )
    assert sid == "rules.operating-modes"


def test_resolve_slot_id_auto_derive_falls_back():
    sid = sync.resolve_slot_id(
        "memory/L1/RULES.md", "## §99 — SOMETHING NEW UNDOCUMENTED", overrides={},
    )
    assert sid.startswith("rules.")
    assert "something" in sid
    assert "new" in sid


def test_auto_derive_strips_section_number():
    sid = sync._auto_derive_slot_id(
        "memory/L1/RULES.md", "## §0 — DOTTRINA TRASPARENZA AI",
    )
    assert sid == "rules.dottrina-trasparenza-ai"


def test_auto_derive_handles_hard_rule_pattern():
    sid = sync._auto_derive_slot_id(
        "memory/L1/RULES.md", "## HARD RULE — Policy authority",
    )
    # "HARD RULE — " is stripped.
    assert sid == "rules.policy-authority"


# ---------------------------------------------------------------------------
# Section composition
# ---------------------------------------------------------------------------

def test_compose_section_doctrine_passes_through():
    s = sync.Section(
        heading="## §0 — DOCTRINE",
        body="<!-- IMMUTABILE -->\n\nVerbatim body.\n",
        marker="IMMUTABILE",
        is_doctrine=True,
    )
    out = sync._compose_section("memory/L1/RULES.md", s, overrides={})
    assert "Verbatim body." in out
    assert "{{slot:" not in out


def test_compose_section_immutable_but_not_doctrine_becomes_slot():
    """IMMUTABILE marker but not in DOCTRINE_SECTIONS — must slotify."""
    s = sync.Section(
        heading="## HARD RULE — Some rule",
        body="<!-- IMMUTABILE -->\n\nMario-specific body.\n",
        marker="IMMUTABILE",
        is_doctrine=False,
    )
    out = sync._compose_section("memory/L1/RULES.md", s, overrides={})
    assert "{{slot:" in out
    assert "Mario-specific body." not in out  # body NOT propagated
    assert "<!-- IMMUTABILE -->" in out       # marker preserved


def test_compose_section_reviewable_becomes_slot():
    s = sync.Section(
        heading="## §2 — MODES",
        body="<!-- REVIEWABLE -->\n\nSome bodies here.\n",
        marker="REVIEWABLE",
        is_doctrine=False,
    )
    out = sync._compose_section("memory/L1/RULES.md", s, overrides={})
    assert "{{slot:rules.modes}}" in out
    assert "<!-- REVIEWABLE -->" in out
    assert "Some bodies here." not in out


def test_compose_section_force_slot_overrides_doctrine():
    """force_slot=True (used for character-bible) slotifies even doctrine sections."""
    s = sync.Section(
        heading="## Some Section",
        body="\n\nBody.\n",
        marker=None,
        is_doctrine=True,
    )
    out = sync._compose_section(
        "memory/L2/character-bible/<slug>.md", s, overrides={}, force_slot=True,
    )
    assert "{{slot:" in out


def test_compose_section_uses_ask_hint_when_present():
    overrides = {
        "sections": {
            "memory/L1/RULES.md": {
                "## §2 — MODES": {
                    "slot_id": "rules.operating-modes",
                    "ask": "Three operating modes — names and posture per mode.",
                },
            },
        },
    }
    s = sync.Section(
        heading="## §2 — MODES",
        body="<!-- REVIEWABLE -->\n\nOriginal.\n",
        marker="REVIEWABLE",
        is_doctrine=False,
    )
    out = sync._compose_section("memory/L1/RULES.md", s, overrides)
    assert "<!-- ASK: Three operating modes" in out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_find_unresolved_placeholders(tmp_path: Path):
    (tmp_path / "memory" / "L1").mkdir(parents=True)
    f = tmp_path / "memory/L1/RULES.md"
    f.write_text("Body with {{slot:rules.operating-modes}} unresolved.\n")
    issues = sync.find_unresolved_placeholders(tmp_path, ["memory/L1/RULES.md"])
    assert len(issues) == 1
    assert "1 placeholder(s)" in issues[0]


def test_find_unresolved_placeholders_clean_source_returns_empty(tmp_path: Path):
    (tmp_path / "memory" / "L1").mkdir(parents=True)
    f = tmp_path / "memory/L1/RULES.md"
    f.write_text("All filled in.\n")
    assert sync.find_unresolved_placeholders(tmp_path, ["memory/L1/RULES.md"]) == []
