#!/usr/bin/env python3
"""Sync the framework's persona template from a populated reference instance.

Reads the L1/L2 files of a reference JC instance and regenerates
`templates/init-instance/` so that:

  - Sections classified as constitutional doctrine (in DOCTRINE_SECTIONS) ship
    verbatim into the framework template — these are the universal invariants
    of the persona experiment (§0 transparency doctrine, §1 trust model, §9
    self-disclosure doctrine, §11 don't-reveal-the-rule, §14 memory access,
    §16 double-block, §18 self-check, §19 final principle, §21 anti-submission).
  - Sections marked <!-- IMMUTABILE --> but NOT in DOCTRINE_SECTIONS are
    treated as operator-locked-to-the-source-instance and become slot
    placeholders (e.g. Mario's HARD RULE on policy authority is specific to
    his Filippo+scovai setup; downstream instances need their own).
  - Sections marked <!-- REVIEWABLE --> or <!-- OPEN --> become slot
    placeholders. Their body is replaced with {{slot:<id>}} + an <!-- ASK -->
    hint. The slot id is resolved from `templates/persona-interview/
    slot-overrides.yaml` (curated) or auto-derived from the heading.

Invocation:

    python scripts/sync_persona_template.py --from /opt/<agent> [--write]

By default the script prints what it would do (dry-run). Pass `--write` to
actually update `templates/init-instance/`.

The script is agent-agnostic — no path is hardcoded. It refuses to write if
the source instance has unresolved {{slot:...}} placeholders (the source
must be fully populated for the sync to produce a valid template).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import yaml  # type: ignore
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

# Make `lib/` importable so we can use persona_macros without a full install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import persona_macros  # noqa: E402


# ---------------------------------------------------------------------------
# Constitutional doctrine — sections that ship verbatim into the framework
# template because they encode universal invariants of the persona experiment.
# Anything NOT in this list, even if marked <!-- IMMUTABILE --> in the source,
# is treated as operator-locked-to-the-source and becomes a slot placeholder.
# ---------------------------------------------------------------------------

DOCTRINE_SECTIONS: dict[str, list[re.Pattern[str]]] = {
    "memory/L1/RULES.md": [
        re.compile(r"^## §0 — "),
        re.compile(r"^## §0\.1 — "),
        re.compile(r"^## §0\.2 — "),
        re.compile(r"^## §1 — TRUST MODEL"),
        re.compile(r"^## §9 — SELF-DISCLOSURE DOCTRINE"),
        re.compile(r"^## §11 — REGOLA DEL"),
        re.compile(r"^## §14 — MEMORY ACCESS CONTROL"),
        re.compile(r"^## §16 — AZIONI A DOPPIO BLOCCO"),
        re.compile(r"^## §18 — SELF-CHECK FINALE"),
        re.compile(r"^## §19 — PRINCIPIO FINALE"),
        re.compile(r"^## §21 — ANTI-SUBMISSION LOOP"),
    ],
    "memory/L1/IDENTITY.md": [
        re.compile(r"^## Stato AI"),
        re.compile(r"^## Obiettivo gerarchico"),
        re.compile(r"^## Principio supremo"),
        re.compile(r"^## Auto-narrazione"),
        re.compile(r"^## Test della frase"),
        re.compile(r"^## CONTINUITY"),
    ],
    "memory/L1/USER.md": [
        # USER.md is largely operator-authored; no universal doctrine.
    ],
}


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

MARKER_IMMUTABILE = "<!-- IMMUTABILE -->"
MARKER_REVIEWABLE = "<!-- REVIEWABLE -->"
MARKER_OPEN = "<!-- OPEN -->"


# ---------------------------------------------------------------------------
# Section parsing
# ---------------------------------------------------------------------------

@dataclass
class Section:
    """One ## heading + body."""
    heading: str           # full heading line, e.g. "## §0 — DOTTRINA TRASPARENZA AI"
    body: str              # everything between this heading and the next ##
    marker: str | None     # IMMUTABILE | REVIEWABLE | OPEN | None
    is_doctrine: bool      # in DOCTRINE_SECTIONS for the source file


def split_into_sections(content: str) -> tuple[str, list[Section]]:
    """Split markdown into (preamble, sections).

    Preamble is everything before the first ##. Sections are split on level-2
    headings. Each section's body includes the trailing whitespace until the
    next heading (or end of file).
    """
    lines = content.splitlines(keepends=True)
    preamble_lines: list[str] = []
    sections: list[Section] = []
    current_heading: str | None = None
    current_body: list[str] = []

    def flush() -> None:
        if current_heading is not None:
            heading = current_heading
            body = "".join(current_body)
            marker = _detect_marker(body)
            sections.append(Section(
                heading=heading,
                body=body,
                marker=marker,
                is_doctrine=False,  # filled in later
            ))

    for line in lines:
        if line.startswith("## "):
            flush()
            current_heading = line.rstrip("\n")
            current_body = []
        elif current_heading is None:
            preamble_lines.append(line)
        else:
            current_body.append(line)
    flush()
    return "".join(preamble_lines), sections


def _detect_marker(body: str) -> str | None:
    """Look at the first 3 non-empty lines of the body for a marker comment."""
    seen = 0
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if MARKER_IMMUTABILE in stripped:
            return "IMMUTABILE"
        if MARKER_REVIEWABLE in stripped:
            return "REVIEWABLE"
        if MARKER_OPEN in stripped:
            return "OPEN"
        seen += 1
        if seen >= 3:
            break
    return None


def annotate_doctrine(file_rel: str, sections: list[Section]) -> None:
    """Set is_doctrine=True for sections matching DOCTRINE_SECTIONS for this file."""
    patterns = DOCTRINE_SECTIONS.get(file_rel, [])
    for s in sections:
        for p in patterns:
            if p.search(s.heading):
                s.is_doctrine = True
                break


# ---------------------------------------------------------------------------
# Slot id resolution
# ---------------------------------------------------------------------------

# File-level prefix used for auto-derived slot ids.
FILE_PREFIX = {
    "memory/L1/RULES.md": "rules",
    "memory/L1/IDENTITY.md": "identity",
    "memory/L1/USER.md": "user",
    "memory/L1/JOURNAL.md": "journal",
    "memory/L1/HOT.md": "hot",
    "memory/L2/character-bible/<slug>.md": "characterbible",
    "memory/L2/cv/<slug>.md": "cv",
    "CONTRIBUTING.md": "contributing",
    "ops/self_model.yaml": "self_model",
}


def load_slot_overrides(framework_root: Path) -> dict:
    """Load slot-overrides.yaml (curated id mapping). Returns empty dict if absent."""
    path = framework_root / "templates" / "persona-interview" / "slot-overrides.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def resolve_slot_id(file_rel: str, heading: str, overrides: dict) -> str:
    """Return curated slot id (from overrides) or auto-derive from heading."""
    file_overrides = (overrides.get("sections") or {}).get(file_rel, {})
    if heading in file_overrides:
        entry = file_overrides[heading]
        if isinstance(entry, dict) and "slot_id" in entry:
            return entry["slot_id"]
    return _auto_derive_slot_id(file_rel, heading)


def _auto_derive_slot_id(file_rel: str, heading: str) -> str:
    """Generate a fallback slot id from heading text."""
    prefix = FILE_PREFIX.get(file_rel, "unknown")
    # Strip "## ", "§N — ", and similar prefixes, then kebab-case the rest.
    text = re.sub(r"^##\s*", "", heading)
    text = re.sub(r"^§[\d.]+\s*[—–-]\s*", "", text)
    # Strip ALLCAPS prefix like "HARD RULE — " or "HARD NO — ".
    text = re.sub(r"^[A-Z]+(?:\s+[A-Z]+)*\s*[—–-]\s*", "", text)
    # Lowercase, replace non-alphanumeric with hyphens, collapse hyphens.
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return f"{prefix}.{text}"


def resolve_ask_hint(file_rel: str, heading: str, overrides: dict) -> str | None:
    """Optional <!-- ASK: ... --> hint text from overrides."""
    file_overrides = (overrides.get("sections") or {}).get(file_rel, {})
    entry = file_overrides.get(heading)
    if isinstance(entry, dict):
        return entry.get("ask")
    return None


def resolve_english_heading(file_rel: str, heading: str, overrides: dict) -> str:
    """Return the framework's English heading for this section, or the source heading.

    Non-doctrine sections in the framework template carry English headings even
    though the source instance may use another language. The override file
    supplies the translation. Falls back to the source heading if no override.
    """
    file_overrides = (overrides.get("sections") or {}).get(file_rel, {})
    entry = file_overrides.get(heading)
    if isinstance(entry, dict) and "heading" in entry:
        return entry["heading"]
    return heading


# ---------------------------------------------------------------------------
# Canonical English doctrine — loaded from templates/persona-interview/doctrine-en.md
# ---------------------------------------------------------------------------

_SECTION_NUMBER_RE = re.compile(r"^##\s+§([\d.]+)\s+—")


def load_english_doctrine(framework_root: Path) -> dict[str, tuple[str, str]]:
    """Parse doctrine-en.md and return {key: (heading_line, body_text)}.

    Two key types:
      - "§<number>" sections (RULES.md doctrine): keyed by the §-number string,
        e.g. "0", "0.1", "1", "9", "21".
      - Named sections (IDENTITY.md doctrine): keyed by the heading text after
        "## ", e.g. "AI Status", "Continuity", "Supreme principle".

    A section can be looked up by either pathway depending on whether the
    caller has a §-number or an English heading.
    """
    path = framework_root / "templates" / "persona-interview" / "doctrine-en.md"
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    _, sections = split_into_sections(text)
    out: dict[str, tuple[str, str]] = {}
    for s in sections:
        match = _SECTION_NUMBER_RE.match(s.heading)
        if match:
            out[match.group(1)] = (s.heading, s.body)
        else:
            # Strip "## " prefix; trim trailing whitespace.
            name = s.heading[3:].strip()
            out[name] = (s.heading, s.body)
    return out


def section_number(heading: str) -> str | None:
    """Extract '§<number>' suffix as a stable key. '## §0.1 — FOO' -> '0.1'."""
    m = _SECTION_NUMBER_RE.match(heading)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Composition — build the synced template content
# ---------------------------------------------------------------------------

@dataclass
class CompositeFile:
    """Output for one synced file."""
    rel_path: str
    content: str


def compose_rules_md(
    source_text: str,
    framework_boilerplate_rules_md: str,
    overrides: dict,
    english_doctrine: dict[str, tuple[str, str]] | None = None,
) -> str:
    """Synthesize the framework template's RULES.md.

    Doctrine sections come from the framework's own english_doctrine (loaded
    from templates/persona-interview/doctrine-en.md), keyed by §-number. The
    source instance is consulted ONLY for §-section ordering and to discover
    which non-doctrine sections exist — its doctrine bodies are not copied.

    Non-doctrine sections become English-headed slot placeholders, with the
    English heading + slot id + ASK hint resolved from slot-overrides.yaml.

    After the §-spine, the framework operational rules tail (Instance
    awareness / Runtime checks / Work routing / Conversation transcripts /
    HOT.md structure) is appended — JC-runtime guidance shared by every
    instance regardless of persona.
    """
    if english_doctrine is None:
        english_doctrine = {}

    preamble, sections = split_into_sections(source_text)
    annotate_doctrine("memory/L1/RULES.md", sections)

    out: list[str] = []
    out.append(_synthetic_frontmatter("RULES", "Operative Constitution"))
    out.append("\n# RULES — Operative Constitution\n\n")
    out.append("This file is the operative constitution of the persona instance. "
               "Sections marked `<!-- IMMUTABILE -->` are universal invariants of "
               "the persona experiment and ship verbatim from the framework's "
               "canonical doctrine (`templates/persona-interview/doctrine-en.md`). "
               "Sections marked `<!-- REVIEWABLE -->` or `<!-- OPEN -->` are "
               "operator-authored — `jc persona interview` fills the "
               "`{{slot:...}}` placeholders below. Macros like "
               "`{{persona.full_name}}` / `{{principal.name}}` / `{{employer.name}}` "
               "are bound to per-instance values at scaffold time.\n\n")

    section_re = re.compile(r"^## §\d")
    seen_sections = 0
    missing_doctrine: list[str] = []
    for s in sections:
        if not section_re.match(s.heading):
            continue
        seen_sections += 1

        if s.is_doctrine and s.marker == "IMMUTABILE":
            # Pull the doctrine body from the framework's English doctrine,
            # NOT from the source instance.
            num = section_number(s.heading)
            if num and num in english_doctrine:
                doc_heading, doc_body = english_doctrine[num]
                out.append(doc_heading + "\n" + doc_body)
            else:
                missing_doctrine.append(s.heading)
                # Fall back: emit a warning placeholder so the framework
                # template never silently loses a doctrine section.
                out.append(s.heading + "\n<!-- IMMUTABILE -->\n\n"
                           f"<!-- TODO: doctrine body for §{num} missing from "
                           f"templates/persona-interview/doctrine-en.md -->\n\n")
        else:
            out.append(_compose_section("memory/L1/RULES.md", s, overrides))

    if seen_sections == 0:
        raise RuntimeError(
            "No §-numbered sections found in source RULES.md — wrong shape?"
        )

    if missing_doctrine:
        print(f"warning: {len(missing_doctrine)} doctrine section(s) present "
              f"in source but missing from doctrine-en.md:", file=sys.stderr)
        for h in missing_doctrine:
            print(f"  {h}", file=sys.stderr)

    if framework_boilerplate_rules_md:
        out.append("\n---\n\n")
        out.append("# Framework operational rules\n\n")
        out.append("Boilerplate JC operational guidance shared by every instance. "
                   "Does NOT participate in the persona constitution above.\n\n")
        out.append(framework_boilerplate_rules_md)

    return "".join(out)


def compose_identity_md(
    source_text: str,
    overrides: dict,
    macro_subs: list[persona_macros.Substitution] | None = None,
    english_doctrine: dict[str, tuple[str, str]] | None = None,
) -> str:
    """Synthesize IDENTITY.md template.

    Doctrine sections (AI Status, Hierarchical objective, Supreme principle,
    Self-narration, Sentence test, Continuity) come from doctrine-en.md
    keyed by their English heading. Non-doctrine sections become English-
    headed slot placeholders.

    The English heading per Italian source heading is supplied by
    slot-overrides.yaml under the "memory/L1/IDENTITY.md" entries — both
    for slot sections (with slot_id + heading) and doctrine sections
    (heading-only mapping; doctrine body looked up in english_doctrine).
    """
    if english_doctrine is None:
        english_doctrine = {}

    preamble, sections = split_into_sections(source_text)
    annotate_doctrine("memory/L1/IDENTITY.md", sections)

    out: list[str] = []
    out.append(_synthetic_frontmatter("IDENTITY", "Identity"))
    out.append("\n# IDENTITY\n\n")
    out.append("Persona declaration. Doctrine sections (AI Status, "
               "Hierarchical objective, Supreme principle, Self-narration, "
               "Sentence test, Continuity) ship verbatim from the framework's "
               "canonical English doctrine. Other sections are filled by "
               "`jc persona interview`. Macros bind at scaffold time.\n\n")

    for s in sections:
        # Skip ARCHIVIO sections — those are source-instance audit history.
        if "ARCHIVIO" in s.heading:
            continue

        if s.is_doctrine and s.marker == "IMMUTABILE":
            # Look up doctrine body in doctrine-en.md by the English heading.
            english_heading = resolve_english_heading(
                "memory/L1/IDENTITY.md", s.heading, overrides,
            )
            doctrine_key = english_heading[3:].strip() if english_heading.startswith("## ") else english_heading
            if doctrine_key in english_doctrine:
                doc_heading, doc_body = english_doctrine[doctrine_key]
                out.append(doc_heading + "\n" + doc_body)
            else:
                # Fall back: emit source heading + macro-substituted body.
                # (Phase 2.6 — IDENTITY doctrine port should have closed this gap;
                # this branch is for sections that escaped the port.)
                body = s.body
                if macro_subs:
                    body = persona_macros.apply_substitutions(body, macro_subs)
                out.append(s.heading + "\n" + body)
                print(f"warning: IDENTITY doctrine '{s.heading}' missing from "
                      f"doctrine-en.md (looked up '{doctrine_key}'); fell back "
                      f"to source body with macro substitution",
                      file=sys.stderr)
        else:
            out.append(_compose_section("memory/L1/IDENTITY.md", s, overrides))

    return "".join(out)


def compose_user_md(source_text: str, overrides: dict) -> str:
    """Synthesize USER.md — entirely operator-authored; ship section spine as slots."""
    preamble, sections = split_into_sections(source_text)
    annotate_doctrine("memory/L1/USER.md", sections)

    out: list[str] = []
    out.append(_synthetic_frontmatter("USER", "User profile — principal"))
    out.append("\n# USER — principal\n\n")
    out.append("Verified principal identity, role-confidentiality lexicon, "
               "Founder-Mode definition, downgrade triggers, channel discipline, "
               "and standing rules tied to the principal. All operator-authored "
               "via `jc persona interview`.\n\n")

    for s in sections:
        if "ARCHIVIO" in s.heading:
            continue
        out.append(_compose_section("memory/L1/USER.md", s, overrides))

    return "".join(out)


def compose_journal_md(framework_root: Path) -> str:
    """JOURNAL.md template comes from the framework's English preamble file.

    `templates/persona-interview/journal-preamble-en.md` is hand-authored
    canonical English content for the journal contract — not derived from
    any reference instance. Includes the standard frontmatter, the contract
    text (scope, voice, append-only rules, lifecycle, triggers, linked
    artifacts), the entry schema, and an empty `## Entries` section.
    """
    path = framework_root / "templates" / "persona-interview" / "journal-preamble-en.md"
    if not path.exists():
        raise RuntimeError(
            "framework's journal-preamble-en.md is missing — required for sync"
        )
    return path.read_text(encoding="utf-8").rstrip() + "\n\n_(no entries yet)_\n"


def compose_hot_md() -> str:
    """HOT.md template — three fixed sections, empty bodies. Already framework-shipped."""
    return """---
slug: HOT
title: Hot Cache (rolling 7 days)
layer: L1
type: hot
state: draft
created: TODO
updated: TODO
last_verified: ""
tags: [hot, recent, rolling]
links: []
---

# Hot cache — rolling 7-day context

What's alive right now. Three fixed sections per `RULES.md` → "HOT.md
structure". Hard cap: 400 lines total (target <300). Each item ≤100 words.
Newest first within each section.

## What shipped

-

## Immediate open threads

-

## Known nuisances

-
"""


def compose_character_bible_md(source_text: str, overrides: dict) -> str:
    """Character bible template — preserve section spine, replace all bodies with slots."""
    preamble, sections = split_into_sections(source_text)
    out: list[str] = []
    out.append("""---
slug: character-bible-{{slot:characterbible.slug}}
title: Character Bible — {{slot:characterbible.full-name}}
layer: L2
type: character-bible
state: draft
created: TODO
updated: TODO
last_verified: ""
tags: [character, persona, identity]
links: [IDENTITY]
---

<!-- REVIEWABLE -->
# Character Bible

Deep persona content. Filled by `jc persona interview` (Mario-style guided
prompts). Curated jointly with the operator; never auto-modified by
`jc self-model`.

""")
    for s in sections:
        out.append(_compose_section(
            "memory/L2/character-bible/<slug>.md", s, overrides,
            force_slot=True,
        ))
    return "".join(out)


def compose_cv_md(source_text: str, overrides: dict) -> str:
    """CV template — preserve section spine, replace bodies with slots."""
    preamble, sections = split_into_sections(source_text)
    out: list[str] = []
    out.append("""# {{slot:cv.full-name}}

External-facing CV. Filled by `jc persona interview`.

---

""")
    for s in sections:
        out.append(_compose_section(
            "memory/L2/cv/<slug>.md", s, overrides,
            force_slot=True,
        ))
    return "".join(out)


def compose_self_model_yaml() -> str:
    """ops/self_model.yaml template — disabled by default, knobs documented."""
    return """# Autonomous self-model loop config.
# See `lib/self_model/` (framework) for the implementation.
# Default: disabled. Enable + start in dry_run mode; promote to propose/apply
# only after observing dry-run signals look sensible.

enabled: false
mode: dry_run            # dry_run | propose | apply

look_back_days: 7        # event window the corpus reads from state/transcripts/
min_evidence_count: 2    # min distinct evidence items required for a proposal
confidence_threshold: 0.85
proposal_cooldown_days: 30

require_dkim_for_rules: true
require_dkim_for_identity: true
require_dkim_for_journal: false   # journal is auto-apply scope (append-only)

scan_weekly_cron: "0 9 * * 0"     # weekly sweep cadence (when scan_weekly enabled)

proposer_model: claude-sonnet-4-6

# Telegram chat id for proposal notifications (optional).
# notify_chat_id: "123456789"

detectors:
  filippo_correction: false   # principal corrections in user messages
  hot_flag: false             # `#self-observation`-tagged HOT.md blocks
  direct_request: false       # explicit "review yourself" requests
  episode_flag: false         # agent's own self-recognition keywords
  scan_weekly: false          # weekly pattern aggregation across journal entries
"""


def compose_contributing_md(source_text: str | None) -> str:
    """CONTRIBUTING.md — verbatim copy of source with principal/instance refs slotified.

    For Phase 2, ship a generic skeleton (operator can replace with theirs).
    A future phase can do precise reference-stripping.
    """
    return """# CONTRIBUTING — persona instance

This instance treats the operative constitution like code: diffable, versioned,
reviewed. Constitutional changes follow a deliberate flow.

## Branches

- **`main`** — stable, contains the current operative constitution.
- **`feat/§N-<topic>`** — feature branches for non-trivial constitutional
  additions (a new section, a policy change, a refactor). Merged via PR
  review + explicit principal approval.

## Commit Message Format

[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <subject>

<body>

<footer>
```

Types:

- `chore:` — setup, tooling, maintenance.
- `policy:` — operative policy change or constitution section update (§1+).
- `memory:` — L1/L2 memory structure or entry updates.
- `docs:` — documentation.
- `fix:` — bug fixes or clarifications to existing rules.

## Tags

Version tags `vX.Y` mark constitution releases:

- **Major (X):** breaking changes to IMMUTABILE sections (trust model, modes,
  boundaries).
- **Minor (Y):** new sections or significant policy additions.

## Policy changes

Constitution updates require:

1. Proposed in conversation or email (principal → agent).
2. Draft on a feature branch (if complex).
3. Explicit principal approval (per `RULES.md` enactment marker).
4. Committed with `policy:` type and a reference to the approval.
5. Tagged if a version bump is warranted.

## What NOT to commit

- `.env` (credentials, API keys).
- `state/` (transcripts, drafts, gateway logs).
- `memory/index.sqlite` (FTS index, auto-generated).
- `heartbeat/state/`, `voice/tmp/` (runtime output).
"""


def _compose_section(
    file_rel: str,
    s: Section,
    overrides: dict,
    *,
    force_slot: bool = False,
    macro_subs: list[persona_macros.Substitution] | None = None,
) -> str:
    """Render one section into the composite output.

    Non-doctrine path: emit English heading (from override) + marker + slot
    placeholder. The English heading decouples the framework template from
    the source's language; the slot id is the stable English semantic key.

    Doctrine path (only used by callers who don't have an English doctrine
    for this section yet — RULES.md doctrine goes through the doctrine-en.md
    branch in compose_rules_md): copy verbatim, with macro substitution.
    Macro substitution is the temporary bridge until the doctrine is ported
    to the framework's own English doctrine file.
    """
    if s.is_doctrine and s.marker == "IMMUTABILE" and not force_slot:
        body = s.body
        if macro_subs:
            body = persona_macros.apply_substitutions(body, macro_subs)
        return s.heading + "\n" + body

    english_heading = resolve_english_heading(file_rel, s.heading, overrides)
    slot_id = resolve_slot_id(file_rel, s.heading, overrides)
    ask_hint = resolve_ask_hint(file_rel, s.heading, overrides)
    marker = s.marker or "REVIEWABLE"

    out = [english_heading, "\n", f"<!-- {marker} -->\n\n"]
    if ask_hint:
        out.append(f"<!-- ASK: {ask_hint} -->\n")
    out.append(f"{{{{slot:{slot_id}}}}}\n\n")
    return "".join(out)


def _synthetic_frontmatter(slug: str, title: str) -> str:
    """Standard YAML frontmatter for memory files."""
    return f"""---
slug: {slug}
title: {title}
layer: L1
type: {slug.lower()}
state: draft
created: TODO
updated: TODO
last_verified: ""
tags: [{slug.lower()}]
links: []
---
"""


# ---------------------------------------------------------------------------
# Validation — refuse to sync a source instance with unresolved placeholders
# ---------------------------------------------------------------------------

PLACEHOLDER_RE = re.compile(r"\{\{slot:[^}]+\}\}")


def find_unresolved_placeholders(source_dir: Path, files: Iterable[str]) -> list[str]:
    """Return list of 'file: count' strings for files containing {{slot:...}}."""
    issues: list[str] = []
    for rel in files:
        path = source_dir / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        matches = PLACEHOLDER_RE.findall(text)
        if matches:
            issues.append(f"{rel}: {len(matches)} placeholder(s)")
    return issues


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

CANONICAL_SOURCE_FILES = [
    "memory/L1/RULES.md",
    "memory/L1/IDENTITY.md",
    "memory/L1/USER.md",
    "memory/L1/JOURNAL.md",
]

# Optional source files (sync if present, skip silently otherwise).
OPTIONAL_SOURCE_FILES = [
    "memory/L2/character-bible/*.md",
    "memory/L2/cv/*.md",
    "CONTRIBUTING.md",
    "ops/self_model.yaml",
]


def sync(
    source_dir: Path,
    framework_root: Path,
    write: bool,
    source_macros_path: Path | None = None,
) -> int:
    """Run the sync. Returns 0 on success, non-zero on validation failure."""
    if not source_dir.is_dir():
        print(f"error: source dir does not exist: {source_dir}", file=sys.stderr)
        return 2

    # 1. Validate the source has no unresolved placeholders.
    issues = find_unresolved_placeholders(source_dir, CANONICAL_SOURCE_FILES)
    if issues:
        print("Source instance has unresolved placeholders — refusing to sync:",
              file=sys.stderr)
        for line in issues:
            print(f"  {line}", file=sys.stderr)
        return 3

    # 2. Load slot overrides.
    overrides = load_slot_overrides(framework_root)
    if not overrides:
        print("note: no slot-overrides.yaml found — using auto-derived slot ids only",
              file=sys.stderr)

    # 2.5. Load doctrine macro substitutions. After Phase 2.6 these are only
    # used by the IDENTITY.md doctrine fallback (still sourced from the
    # reference instance) and any other compose path that hasn't yet been
    # ported to a framework-side English doctrine source.
    if source_macros_path is None:
        source_macros_path = (
            framework_root / "templates" / "persona-interview"
            / "macros-from-reference.yaml"
        )
    macro_subs: list[persona_macros.Substitution] = []
    if source_macros_path.exists():
        macro_subs = persona_macros.load_substitutions(source_macros_path)
        print(f"loaded {len(macro_subs)} macro substitutions from "
              f"{source_macros_path}", file=sys.stderr)
    else:
        print(f"note: no macros file at {source_macros_path} — doctrine "
              f"fallback paths will ship verbatim with source proper nouns",
              file=sys.stderr)

    # 2.6. Load the framework's canonical English doctrine. This decouples
    # doctrine content from any reference instance — doctrine-en.md is
    # hand-authored as a research artifact in its own right.
    english_doctrine = load_english_doctrine(framework_root)
    print(f"loaded {len(english_doctrine)} English doctrine section(s) from "
          f"templates/persona-interview/doctrine-en.md", file=sys.stderr)

    # 3. Read framework's operational rules tail (Instance awareness / Runtime
    # checks / Work routing / Conversation transcripts / HOT.md structure).
    boilerplate_rules = _extract_framework_boilerplate(framework_root)

    # 4. Compose each output file.
    outputs: list[CompositeFile] = []

    rules_src = (source_dir / "memory/L1/RULES.md").read_text(encoding="utf-8")
    outputs.append(CompositeFile(
        "memory/L1/RULES.md",
        compose_rules_md(rules_src, boilerplate_rules, overrides, english_doctrine),
    ))

    identity_src = (source_dir / "memory/L1/IDENTITY.md").read_text(encoding="utf-8")
    outputs.append(CompositeFile(
        "memory/L1/IDENTITY.md",
        compose_identity_md(identity_src, overrides, macro_subs, english_doctrine),
    ))

    user_src = (source_dir / "memory/L1/USER.md").read_text(encoding="utf-8")
    outputs.append(CompositeFile(
        "memory/L1/USER.md",
        compose_user_md(user_src, overrides),
    ))

    # JOURNAL preamble is now hand-authored English in the framework, not
    # derived from the source instance.
    outputs.append(CompositeFile(
        "memory/L1/JOURNAL.md",
        compose_journal_md(framework_root),
    ))

    outputs.append(CompositeFile("memory/L1/HOT.md", compose_hot_md()))

    # Character bible — sync from the first available L2/character-bible/*.md.
    cb_dir = source_dir / "memory/L2/character-bible"
    if cb_dir.is_dir():
        cb_files = sorted(cb_dir.glob("*.md"))
        if cb_files:
            outputs.append(CompositeFile(
                "memory/L2/character-bible/<slug>.md",
                compose_character_bible_md(
                    cb_files[0].read_text(encoding="utf-8"), overrides,
                ),
            ))

    cv_dir = source_dir / "memory/L2/cv"
    if cv_dir.is_dir():
        cv_files = sorted(cv_dir.glob("*.md"))
        if cv_files:
            outputs.append(CompositeFile(
                "memory/L2/cv/<slug>.md",
                compose_cv_md(cv_files[0].read_text(encoding="utf-8"), overrides),
            ))

    outputs.append(CompositeFile("ops/self_model.yaml", compose_self_model_yaml()))
    outputs.append(CompositeFile("CONTRIBUTING.md", compose_contributing_md(None)))

    # 5. Write or report.
    template_root = framework_root / "templates" / "init-instance"
    for out in outputs:
        target = template_root / out.rel_path
        if write:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(out.content, encoding="utf-8")
            print(f"WROTE  {out.rel_path}  ({len(out.content)} bytes)")
        else:
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            status = "MODIFY" if existing else "CREATE"
            print(f"{status}  {out.rel_path}  "
                  f"(would write {len(out.content)} bytes; "
                  f"current {len(existing)} bytes)")

    if not write:
        print()
        print("Dry-run only. Pass --write to actually update templates/init-instance/.")
    return 0


def _extract_framework_boilerplate(framework_root: Path) -> str:
    """Read the framework-baseline operational rules tail.

    Sourced from `templates/persona-interview/framework-rules-tail.md` — a
    file the sync script never writes, so it stays stable across runs. The
    tail contains JC-runtime guidance shared by every instance (Instance
    awareness, Runtime checks, Work routing, Conversation transcripts, HOT.md
    structure). It is appended after the persona §-spine in synced RULES.md.

    Reading this from a separate file (instead of the previous
    `templates/init-instance/memory/L1/RULES.md`) prevents the sync output
    from feeding back into its own input on subsequent runs — earlier prototype
    of this script bloated RULES.md from ~30KB to 51KB on the second pass
    because each run re-appended the previous run's spine as 'boilerplate'.
    """
    tail = framework_root / "templates" / "persona-interview" / "framework-rules-tail.md"
    if not tail.exists():
        return ""
    return tail.read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="source_dir", required=True, type=Path,
                        help="Path to the populated reference instance.")
    parser.add_argument("--write", action="store_true",
                        help="Actually update templates/init-instance/. "
                             "Default is dry-run.")
    parser.add_argument("--framework-root", type=Path,
                        default=Path(__file__).resolve().parent.parent,
                        help="Framework repo root (default: this script's parent).")
    parser.add_argument("--source-macros", type=Path, default=None,
                        help="Path to macros-from-reference.yaml. Default: "
                             "templates/persona-interview/macros-from-reference.yaml "
                             "in the framework root.")
    args = parser.parse_args()
    return sync(
        args.source_dir.resolve(),
        args.framework_root.resolve(),
        args.write,
        source_macros_path=args.source_macros.resolve() if args.source_macros else None,
    )


if __name__ == "__main__":
    sys.exit(main())
