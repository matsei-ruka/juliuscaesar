"""Tests for lib/self_model/frozen_sections.py.

The frozen-section list is a constitutional invariant — proposals targeting
these sections must be rejected by the proposer (pre/post LLM) and by the
applier. Locking the list here so accidental edits are caught in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from self_model import frozen_sections as fs  # noqa: E402


# ---------------------------------------------------------------------------
# Constitutional invariants — these specific sections must be IMMUTABILE.
# Adding to the list is a research-design PR; removing requires a doctrine
# change and is intentionally hard.
# ---------------------------------------------------------------------------

REQUIRED_RULES_PATTERNS = [
    "§0 ",                                   # AI transparency doctrine
    "§0\\.1",                                # threshold protocols
    "§0\\.2",                                # agent-self vs character
    "§1 — TRUST MODEL",
    "§9 — SELF-DISCLOSURE DOCTRINE",         # Phase 7 addition
    "§11 — REGOLA DEL",                      # don't-reveal-the-rule (Italian)
    "§14 — MEMORY ACCESS CONTROL",
    "§16 — AZIONI A DOPPIO BLOCCO",          # double-block (Italian)
    "§17 — AUDIT, RATE LIMIT, KILL SWITCH",  # Phase 7 addition (operationally over-protected)
    "§18 — SELF-CHECK FINALE",
    "§19 — PRINCIPIO FINALE",
    "§21 — ANTI-SUBMISSION LOOP",
]


def test_rules_frozen_list_covers_constitutional_invariants():
    """The framework's frozen-section registry must contain every required pattern.

    Adding a section here is a research-design decision (must come with a
    doctrine rationale). Removing one requires explicit test-change +
    doctrine-change PR — these are constitutional invariants.
    """
    raw = "\n".join(fs.FROZEN_SECTIONS_RULES)
    for needle in REQUIRED_RULES_PATTERNS:
        assert needle in raw, (
            f"FROZEN_SECTIONS_RULES missing pattern containing {needle!r} — "
            "this is a constitutional invariant; do not remove without a "
            "doctrine-change PR"
        )


REQUIRED_IDENTITY_PATTERNS = [
    "Ruolo",
    "Funzione operativa",
    "Posizionamento",
    "Stato AI",
    "Obiettivo gerarchico",
    "Principio supremo",
    "Auto-narrazione",                       # Phase 7
    "Test della frase",                      # Phase 7
    "Character",                             # Phase 7 — public character section
]


def test_identity_frozen_list_covers_required_sections():
    raw = "\n".join(fs.FROZEN_SECTIONS_IDENTITY)
    for needle in REQUIRED_IDENTITY_PATTERNS:
        assert needle in raw, f"FROZEN_SECTIONS_IDENTITY missing {needle!r}"


def test_phase7_additions_actually_match_headings():
    """The §9 / §17 / Character entries actually catch the headings they're for."""
    assert fs.is_section_frozen(
        "memory/L1/RULES.md",
        "## §9 — SELF-DISCLOSURE DOCTRINE",
    )
    assert fs.is_section_frozen(
        "memory/L1/RULES.md",
        "## §17 — AUDIT, RATE LIMIT, KILL SWITCH",
    )
    assert fs.is_section_frozen(
        "memory/L1/IDENTITY.md",
        "## Character — public identity",
    )
    assert fs.is_section_frozen(
        "memory/L1/IDENTITY.md",
        "## Character Mario Leone — Identità pubblica",  # the lead-user form
    )


def test_english_aliases_match_doctrine_en_headings():
    """The framework template ships English headings via doctrine-en.md;
    the registry must catch those equivalents alongside the Italian forms."""
    assert fs.is_section_frozen(
        "memory/L1/RULES.md", "## §11 — DON'T-REVEAL-THE-RULE PRINCIPLE",
    )
    assert fs.is_section_frozen(
        "memory/L1/RULES.md", "## §16 — DOUBLE-BLOCK ACTIONS",
    )
    assert fs.is_section_frozen(
        "memory/L1/RULES.md", "## §18 — FINAL SELF-CHECK",
    )
    assert fs.is_section_frozen(
        "memory/L1/RULES.md", "## §19 — FINAL PRINCIPLE",
    )
    assert fs.is_section_frozen("memory/L1/IDENTITY.md", "## Role")
    assert fs.is_section_frozen("memory/L1/IDENTITY.md", "## AI Status")
    assert fs.is_section_frozen("memory/L1/IDENTITY.md", "## Continuity")


# ---------------------------------------------------------------------------
# is_section_frozen — the core gate used by proposer + applier
# ---------------------------------------------------------------------------

def test_is_section_frozen_true_for_doctrine():
    assert fs.is_section_frozen(
        "memory/L1/RULES.md",
        "## §0 — DOTTRINA TRASPARENZA AI",
    )
    assert fs.is_section_frozen(
        "memory/L1/RULES.md",
        "## §21 — ANTI-SUBMISSION LOOP",
    )


def test_is_section_frozen_false_for_non_doctrine():
    assert not fs.is_section_frozen(
        "memory/L1/RULES.md",
        "## §2 — TRE MODE OPERATIVI",
    )
    assert not fs.is_section_frozen(
        "memory/L1/RULES.md",
        "## §10 — RIFIUTO FLUIDO",
    )


def test_is_section_frozen_false_for_unknown_file():
    assert not fs.is_section_frozen(
        "memory/L1/HOT.md",
        "## §0 — anything",
    )


def test_is_section_frozen_handles_missing_section():
    assert not fs.is_section_frozen("memory/L1/RULES.md", None)
    assert not fs.is_section_frozen("memory/L1/RULES.md", "")


# ---------------------------------------------------------------------------
# HTML marker constants
# ---------------------------------------------------------------------------

def test_marker_constants_exist():
    assert fs.MARKER_IMMUTABILE == "<!-- IMMUTABILE -->"
    assert fs.MARKER_REVIEWABLE == "<!-- REVIEWABLE -->"
    assert fs.MARKER_OPEN == "<!-- OPEN -->"
