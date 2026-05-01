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
    "§0 ",            # AI transparency doctrine
    "§0\\.1",         # Threshold protocols
    "§0\\.2",         # Agent-self vs character
    "§1 — TRUST MODEL",
    "§11 — REGOLA DEL",  # Don't-reveal-the-rule (Italian heading)
    "§14 — MEMORY ACCESS CONTROL",
    "§16 — AZIONI A DOPPIO BLOCCO",
    "§18 — SELF-CHECK FINALE",
    "§19 — PRINCIPIO FINALE",
    "§21 — ANTI-SUBMISSION LOOP",
]


def test_rules_frozen_list_covers_constitutional_invariants():
    """The framework's frozen-section registry must contain every required pattern."""
    raw = "\n".join(fs.FROZEN_SECTIONS_RULES)
    for needle in REQUIRED_RULES_PATTERNS:
        assert needle in raw, (
            f"FROZEN_SECTIONS_RULES missing pattern containing {needle!r} — "
            "this is a constitutional invariant; do not remove without a "
            "doctrine-change PR"
        )


def test_identity_frozen_list_covers_required_sections():
    raw = "\n".join(fs.FROZEN_SECTIONS_IDENTITY)
    for needle in ["Stato AI", "Obiettivo gerarchico", "Principio supremo"]:
        assert needle in raw, f"FROZEN_SECTIONS_IDENTITY missing {needle!r}"


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
