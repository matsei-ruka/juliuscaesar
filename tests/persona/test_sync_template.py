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


# ---------------------------------------------------------------------------
# Boilerplate extraction — guards against the self-feedback bug where the
# sync output was read as 'framework boilerplate' on the next run, causing
# the §-spine to be appended each cycle and ballooning RULES.md.
# ---------------------------------------------------------------------------

def test_boilerplate_source_is_not_the_synced_template():
    """The framework operational tail must NOT be read from templates/init-instance/.

    Reading from there would let a previous sync run pollute the next run's
    boilerplate. The function must read from a stable, never-rewritten file.
    """
    framework_root = Path(__file__).resolve().parent.parent.parent
    boilerplate = sync._extract_framework_boilerplate(framework_root)
    # If the function were reading from the synced output, the boilerplate
    # would contain §-numbered headings (the persona spine).
    assert "## §0" not in boilerplate
    assert "## §1" not in boilerplate
    assert "## §2" not in boilerplate
    # Sanity: it should still contain the framework operational guidance.
    assert (
        "Instance awareness" in boilerplate
        or "Conversation transcripts" in boilerplate
        or "Work routing" in boilerplate
    ), "boilerplate is missing the expected framework operational rules"


def test_boilerplate_extraction_idempotent(tmp_path: Path):
    """Calling _extract_framework_boilerplate twice returns identical content."""
    framework_root = Path(__file__).resolve().parent.parent.parent
    a = sync._extract_framework_boilerplate(framework_root)
    b = sync._extract_framework_boilerplate(framework_root)
    assert a == b


# ---------------------------------------------------------------------------
# Phase 7 — nested H3 IMMUTABILE detection
# ---------------------------------------------------------------------------

def test_find_nested_immutable_h3_finds_marker():
    body = """\
Some preamble.

### Principio
<!-- IMMUTABILE -->

Loyalty hierarchy text.

### Other Sub-Section

Not immutable.
"""
    found = sync.find_nested_immutable_h3s(body)
    assert len(found) == 1
    h3, h3_body = found[0]
    assert h3 == "### Principio"
    assert "Loyalty hierarchy text" in h3_body


def test_find_nested_immutable_h3_no_marker_returns_empty():
    body = """\
Some preamble.

### Sub
Just a sub-section, no marker.

### Another Sub

Also no marker.
"""
    assert sync.find_nested_immutable_h3s(body) == []


def test_find_nested_immutable_h3_marker_too_far_after_heading_ignored():
    """The marker must be within the first 3 non-empty lines of the H3 body."""
    body = """\
### Sub

Line one.

Line two.

Line three.

<!-- IMMUTABILE -->
"""
    assert sync.find_nested_immutable_h3s(body) == []


def test_section_number_re_handles_dotted_subnames():
    """§15.Principle should parse as the sub-doctrine key '15.Principle'."""
    assert sync.section_number("## §15.Principle — Insider loyalty hierarchy") == "15.Principle"
    # Existing forms still work.
    assert sync.section_number("## §0 — DOTTRINA TRASPARENZA AI") == "0"
    assert sync.section_number("## §0.1 — PROTOCOLLI") == "0.1"
    assert sync.section_number("## §21 — ANTI-SUBMISSION LOOP") == "21"


def test_load_english_doctrine_includes_subsection_entries():
    """doctrine-en.md ships §15.Principle as a sub-doctrine entry."""
    framework_root = Path(__file__).resolve().parent.parent.parent
    doctrine = sync.load_english_doctrine(framework_root)
    assert "15.Principle" in doctrine
    heading, body = doctrine["15.Principle"]
    assert "INSIDER" in heading.upper() or "LOYALTY" in heading.upper() or "PRINCIPLE" in heading.upper()
    assert "{{principal.name}}" in body  # English principle uses macros


def test_h3_doctrine_suffix_map_covers_principio():
    """The Italian-to-English H3 mapping must catch '### Principio' (Mario's form)."""
    assert sync.slugify_h3("### Principio") == "Principle"
    assert sync.slugify_h3("### Principle") == "Principle"
    assert sync.slugify_h3("### Some Other Heading") is None


def test_synced_rules_md_carries_nested_principle_for_section_15():
    """End-to-end: the framework template's §15 should now include the nested
    `### Principle` doctrine block before the slot placeholder."""
    framework_root = Path(__file__).resolve().parent.parent.parent
    template_rules = framework_root / "templates" / "init-instance" / "memory" / "L1" / "RULES.md"
    text = template_rules.read_text(encoding="utf-8")
    # The §15 heading exists.
    assert "## §15 — INSIDER ROLE BOUNDARIES" in text
    # An H3 Principle block now appears within the framework template's §15.
    assert "### Principle" in text
    # The principle text uses the macro, not literal proper nouns.
    assert "{{principal.name}}" in text


# ---------------------------------------------------------------------------
# Doctrine decoupling — framework's English doctrine is the source of truth,
# not the reference instance's content.
# ---------------------------------------------------------------------------

def test_english_doctrine_loads_with_expected_sections():
    """doctrine-en.md must cover RULES §0/§0.1/§0.2/§1/§9/§11/§14/§16/§18/§19/§21
    + IDENTITY doctrine (AI Status, Hierarchical objective, Supreme principle,
    Self-narration, Sentence test, Continuity)."""
    framework_root = Path(__file__).resolve().parent.parent.parent
    doctrine = sync.load_english_doctrine(framework_root)

    expected_rules = ["0", "0.1", "0.2", "1", "9", "11", "14", "16", "18", "19", "21"]
    for num in expected_rules:
        assert num in doctrine, f"doctrine-en.md missing §{num}"

    expected_identity = [
        "AI Status",
        "Hierarchical objective",
        "Supreme principle",
        "Self-narration",
        "Sentence test",
        "Continuity",
    ]
    for name in expected_identity:
        assert name in doctrine, f"doctrine-en.md missing IDENTITY section '{name}'"


def test_english_doctrine_uses_macros_not_proper_nouns():
    """The framework's English doctrine must use macros, not literal proper
    nouns from any reference instance — otherwise the framework template
    leaks Mario-specific identity into doctrine that's supposed to be portable."""
    framework_root = Path(__file__).resolve().parent.parent.parent
    doctrine_path = framework_root / "templates" / "persona-interview" / "doctrine-en.md"
    text = doctrine_path.read_text(encoding="utf-8")

    # Skip the comment block at the top (it can mention reference names as historical context).
    lines = text.splitlines()
    in_comment = False
    body_lines: list[str] = []
    for line in lines:
        if "<!--" in line:
            in_comment = True
        if not in_comment:
            body_lines.append(line)
        if "-->" in line:
            in_comment = False
    body = "\n".join(body_lines)

    forbidden = [
        "Mario Leone",
        "Filippo Perta",
        "Omnisage LLC",
        # Standalone first names allowed in comment context but not in doctrine prose.
    ]
    for term in forbidden:
        assert term not in body, (
            f"doctrine-en.md body contains literal '{term}' — should be macroed"
        )


def test_sync_does_not_modify_source_instance(tmp_path: Path):
    """Running the sync against a temporary 'reference' must not write to it.

    Build a small synthetic source instance, snapshot all its file mtimes
    + contents, run sync against it, verify nothing changed in the source.
    """
    framework_root = Path(__file__).resolve().parent.parent.parent

    # Minimal source instance.
    src = tmp_path / "src_instance"
    (src / "memory" / "L1").mkdir(parents=True)
    rules = src / "memory/L1/RULES.md"
    rules.write_text("""---
slug: RULES
---

## §0 — DOTTRINA TRASPARENZA AI
<!-- IMMUTABILE -->

Doctrine body.

## §2 — TRE MODE OPERATIVI
<!-- REVIEWABLE -->

Slot body.
""")
    identity = src / "memory/L1/IDENTITY.md"
    identity.write_text("""---
slug: IDENTITY
---

## Ruolo
Stub role body.
""")
    user = src / "memory/L1/USER.md"
    user.write_text("""---
slug: USER
---

## Identità verificata
Stub user body.
""")

    # Snapshot mtimes + contents.
    before = {}
    for path in src.rglob("*"):
        if path.is_file():
            before[path] = (path.stat().st_mtime_ns, path.read_bytes())

    # Run sync — write to a tmp framework root so we don't touch the real one.
    tmp_framework = tmp_path / "framework"
    (tmp_framework / "templates" / "persona-interview").mkdir(parents=True)
    (tmp_framework / "templates" / "init-instance" / "memory" / "L1").mkdir(parents=True)
    # Copy the real framework's persona-interview/ files so the sync has its inputs.
    import shutil
    shutil.copy(
        framework_root / "templates" / "persona-interview" / "doctrine-en.md",
        tmp_framework / "templates" / "persona-interview" / "doctrine-en.md",
    )
    shutil.copy(
        framework_root / "templates" / "persona-interview" / "journal-preamble-en.md",
        tmp_framework / "templates" / "persona-interview" / "journal-preamble-en.md",
    )
    shutil.copy(
        framework_root / "templates" / "persona-interview" / "framework-rules-tail.md",
        tmp_framework / "templates" / "persona-interview" / "framework-rules-tail.md",
    )
    shutil.copy(
        framework_root / "templates" / "persona-interview" / "slot-overrides.yaml",
        tmp_framework / "templates" / "persona-interview" / "slot-overrides.yaml",
    )

    rc = sync.sync(src, tmp_framework, write=True)
    assert rc == 0, "sync exited non-zero"

    # Check source unchanged.
    after = {}
    for path in src.rglob("*"):
        if path.is_file():
            after[path] = (path.stat().st_mtime_ns, path.read_bytes())
    assert before == after, "sync mutated the source instance"
