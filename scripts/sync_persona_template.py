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
) -> str:
    """Synthesize the framework template's RULES.md from Mario's source.

    Strategy: keep ONLY the §-numbered sections (the persona constitution).
    Drop the `# REGOLE TECNICHE E OPERATIVE SPECIFICHE` operational half —
    that's source-instance-specific. After the §-spine, append the framework
    boilerplate (Instance awareness / Runtime checks / Work routing /
    Conversation transcripts / HOT.md structure) preserved from the current
    framework template.
    """
    preamble, sections = split_into_sections(source_text)
    annotate_doctrine("memory/L1/RULES.md", sections)

    out: list[str] = []
    out.append(_synthetic_frontmatter("RULES", "Standing Rules & Costituzione operativa"))
    out.append("\n# RULES — Costituzione Operativa\n\n")
    out.append("This file is the operative constitution of the persona instance. "
               "Sections marked `<!-- IMMUTABILE -->` are universal invariants of "
               "the persona experiment and ship verbatim with the framework. "
               "Sections marked `<!-- REVIEWABLE -->` or `<!-- OPEN -->` are "
               "operator-authored — `jc persona interview` fills the `{{slot:...}}` "
               "placeholders below.\n\n")

    section_re = re.compile(r"^## §\d")
    seen_sections = 0
    for s in sections:
        if not section_re.match(s.heading):
            continue
        seen_sections += 1
        out.append(_compose_section("memory/L1/RULES.md", s, overrides))

    if seen_sections == 0:
        raise RuntimeError(
            "No §-numbered sections found in source RULES.md — wrong shape?"
        )

    # Append framework-boilerplate operational rules from the current template,
    # if available. These are JC-runtime guidance (transcripts, work routing,
    # HOT.md structure) shared by every instance regardless of persona.
    if framework_boilerplate_rules_md:
        out.append("\n---\n\n")
        out.append("# Framework operational rules\n\n")
        out.append("Boilerplate JC operational guidance shared by every instance. "
                   "Does NOT participate in the persona constitution above.\n\n")
        out.append(framework_boilerplate_rules_md)

    return "".join(out)


def compose_identity_md(source_text: str, overrides: dict) -> str:
    """Synthesize IDENTITY.md template — preserve doctrine sections, slotify the rest."""
    preamble, sections = split_into_sections(source_text)
    annotate_doctrine("memory/L1/IDENTITY.md", sections)

    out: list[str] = []
    out.append(_synthetic_frontmatter("IDENTITY", "Identity"))
    out.append("\n# IDENTITY\n\n")
    out.append("Persona declaration. Doctrine sections (auto-narration ban, AI "
               "transparency, hierarchical objective, supreme principle, "
               "continuity) ship verbatim. Other sections are filled by "
               "`jc persona interview`.\n\n")

    for s in sections:
        # Skip ARCHIVIO sections — those are source-instance audit history.
        if "ARCHIVIO" in s.heading:
            continue
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


def compose_journal_md(source_text: str) -> str:
    """JOURNAL.md template = source preamble (the journal contract) + empty entries."""
    # The journal contract (preamble + schema) is itself doctrine — verbatim.
    # Entries section becomes empty.
    # Find the "## Entries" heading; everything before it is the contract.
    entries_match = re.search(r"^## Entries\s*$", source_text, re.MULTILINE)
    if entries_match:
        contract = source_text[: entries_match.end()]
    else:
        contract = source_text
    return contract.rstrip() + "\n\n_(no entries yet)_\n"


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
) -> str:
    """Render one section into the composite output.

    - is_doctrine + IMMUTABILE marker: copy verbatim.
    - everything else (REVIEWABLE / OPEN / IMMUTABILE-but-not-doctrine /
      no-marker, OR force_slot=True): heading + marker + slot placeholder.
    """
    if s.is_doctrine and s.marker == "IMMUTABILE" and not force_slot:
        return s.heading + "\n" + s.body

    slot_id = resolve_slot_id(file_rel, s.heading, overrides)
    ask_hint = resolve_ask_hint(file_rel, s.heading, overrides)
    marker = s.marker or "REVIEWABLE"

    out = [s.heading, "\n", f"<!-- {marker} -->\n\n"]
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


def sync(source_dir: Path, framework_root: Path, write: bool) -> int:
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

    # 3. Read framework's current RULES.md to extract the boilerplate operational
    # rules tail (Instance awareness / Runtime checks / Work routing / etc.).
    boilerplate_rules = _extract_framework_boilerplate(framework_root)

    # 4. Compose each output file.
    outputs: list[CompositeFile] = []

    rules_src = (source_dir / "memory/L1/RULES.md").read_text(encoding="utf-8")
    outputs.append(CompositeFile(
        "memory/L1/RULES.md",
        compose_rules_md(rules_src, boilerplate_rules, overrides),
    ))

    identity_src = (source_dir / "memory/L1/IDENTITY.md").read_text(encoding="utf-8")
    outputs.append(CompositeFile(
        "memory/L1/IDENTITY.md",
        compose_identity_md(identity_src, overrides),
    ))

    user_src = (source_dir / "memory/L1/USER.md").read_text(encoding="utf-8")
    outputs.append(CompositeFile(
        "memory/L1/USER.md",
        compose_user_md(user_src, overrides),
    ))

    journal_path = source_dir / "memory/L1/JOURNAL.md"
    if journal_path.exists():
        outputs.append(CompositeFile(
            "memory/L1/JOURNAL.md",
            compose_journal_md(journal_path.read_text(encoding="utf-8")),
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
    """Pull the operational-rules section from the current framework RULES.md.

    The current template ships with sections like 'Instance awareness',
    'Runtime checks', 'Work routing', 'Conversation transcripts', 'HOT.md
    structure' — the JC-runtime guidance shared by every instance. We preserve
    these in the synced output so persona-aware instances retain them.
    """
    current = framework_root / "templates" / "init-instance" / "memory/L1/RULES.md"
    if not current.exists():
        return ""
    text = current.read_text(encoding="utf-8")
    # Drop the YAML frontmatter and the top-level # heading.
    parts = text.split("\n---\n", 2)
    body = parts[-1] if len(parts) >= 2 else text
    # Drop the first '# Standing rules' top-level heading line.
    body = re.sub(r"^# Standing rules\s*\n", "", body, count=1, flags=re.MULTILINE)
    return body.lstrip()


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
    args = parser.parse_args()
    return sync(args.source_dir.resolve(), args.framework_root.resolve(), args.write)


if __name__ == "__main__":
    sys.exit(main())
