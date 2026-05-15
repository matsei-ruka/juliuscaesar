"""Scaffold opt-in memory features into an instance directory.

Supports the scaffolding flows described in:
- docs/specs/accountabilities.md §Phase 3 (`scaffold_accountabilities`)
- docs/specs/relational-awareness-layer.md §Phase 3 (`scaffold_entities`)
- docs/specs/inter-agent-protocol.md §Phase 3 (`scaffold_inter_agent`)
- docs/specs/adaptive-discovery.md §Phase 3 (`scaffold_adaptive_discovery`)

Each helper copies template files into the operator's instance dir (where
applicable) and prints the constitutional snippet to stdout for manual paste
into `memory/L1/RULES.md`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

_DEFAULT_TEMPLATES_DIR = (
    Path(__file__).resolve().parents[2] / "templates" / "instance"
)

_MANIFEST_SRC = "memory/L1/accountabilities-manifest.md.template"
_MANIFEST_DST = "memory/L1/accountabilities-manifest.md"

_README_SRC = "memory/L2/accountabilities/_README.md"
_README_DST = "memory/L2/accountabilities/_README.md"

_DETAIL_TEMPLATE_SRC = "memory/L2/accountabilities/<slug>.md.template"
_DETAIL_TEMPLATE_DST = "memory/L2/accountabilities/<slug>.md.template"

_RULES_SNIPPET_SRC = "memory/L1/RULES.md.accountability-section.template"

# Relational awareness — entities/
_ENTITIES_DIR = "memory/L2/entities"
_ENTITY_TEMPLATE_FILES = ("<slug>.md.template", "_README.md", "_categories.md")
_PEOPLE_DIR = "memory/L2/people"
_ARCHIVE_PARENT = "memory/L2/_archive"

# Inter-agent protocol
_AUTHORITY_MAP_SRC = "memory/L1/authority-map.md.template"
_AUTHORITY_MAP_DST = "memory/L1/authority-map.md"
_INTER_AGENT_SNIPPET_SRC = "memory/L1/RULES.md.inter-agent-section.template"

# Adaptive discovery
_ADAPTIVE_DISCOVERY_SNIPPET_SRC = (
    "memory/L1/RULES.md.adaptive-discovery-section.template"
)


def _copy_if_missing(src: Path, dst: Path) -> bool:
    if dst.exists():
        print(f"[skip] {dst} already exists — not overwriting")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"[write] {dst}")
    return True


def scaffold_accountabilities(
    instance_dir: Path,
    templates_dir: Path | None = None,
) -> None:
    """Copy accountability templates into instance_dir; print RULES snippet.

    Idempotent: existing files are skipped with a warning. The L2
    accountabilities/ directory is created implicitly via the README copy.

    The constitutional RULES.md snippet is printed to stdout (NOT written) so
    the operator can paste it under their next free §-number — the framework
    refuses to mutate the operator's constitution unprompted.
    """
    templates = templates_dir if templates_dir is not None else _DEFAULT_TEMPLATES_DIR

    manifest_src = templates / _MANIFEST_SRC
    readme_src = templates / _README_SRC
    detail_template_src = templates / _DETAIL_TEMPLATE_SRC
    snippet_src = templates / _RULES_SNIPPET_SRC

    for src in (manifest_src, readme_src, detail_template_src, snippet_src):
        if not src.exists():
            raise FileNotFoundError(f"template missing: {src}")

    _copy_if_missing(manifest_src, instance_dir / _MANIFEST_DST)
    _copy_if_missing(readme_src, instance_dir / _README_DST)
    _copy_if_missing(detail_template_src, instance_dir / _DETAIL_TEMPLATE_DST)

    # Ensure the L2 accountabilities dir exists even if the README was already
    # present (defensive — README copy normally creates it).
    (instance_dir / "memory" / "L2" / "accountabilities").mkdir(
        parents=True, exist_ok=True
    )

    print()
    print("--- BEGIN RULES.md snippet ---")
    print(snippet_src.read_text(encoding="utf-8"), end="")
    print("--- END RULES.md snippet ---")
    print()
    print(
        "Paste the accountability section snippet into your memory/L1/RULES.md "
        "under your next free §-number."
    )


def _today_iso() -> str:
    return date.today().isoformat()


def _entity_slug_from_path(path: Path) -> str:
    return path.stem


def _entity_stub(slug: str, today: str) -> str:
    return (
        "---\n"
        f"slug: {slug}\n"
        f"entity_id: {slug}\n"
        "entity_type: human\n"
        "entity_category: unknown\n"
        f"display_name: {slug}\n"
        "human_authority: \"\"\n"
        "accountabilities_pointer: TBD\n"
        "knowledge_state: inferred\n"
        "classification_confidence: low\n"
        "confidence_basis: migrated from memory/L2/people/ — not yet reviewed\n"
        f"created: {today}\n"
        f"updated: {today}\n"
        f"last_verified: {today}\n"
        "tags: [entities, unknown, migrated]\n"
        "---\n"
        "\n"
        f"# {slug}\n"
        "\n"
        "<!-- Migrated stub. Review and promote when evidence resolves the classification. -->\n"
        "<!-- Original record archived at "
        f"{_ARCHIVE_PARENT}/people-pre-{today}/{slug}.md -->\n"
        "\n"
        "## Identity\n\n…\n\n"
        "## Relationship\n\n…\n\n"
        "## Communication\n\n…\n\n"
        "## Open items\n\n…\n\n"
        "## Notes\n\n…\n"
    )


def _archive_readme(today: str, migrated: list[str]) -> str:
    body = (
        "# people/ archive — pre-entities migration\n"
        "\n"
        f"Captured on {today} by `jc memory scaffold entities --migrate-people`.\n"
        "\n"
        "The previous `memory/L2/people/` directory has been moved here for audit and\n"
        "rollback. The relational awareness layer now owns entity records at\n"
        "`memory/L2/entities/`. Each file archived below has a stub entity record\n"
        "generated at `memory/L2/entities/<slug>.md` with defaults:\n"
        "\n"
        "    entity_category: unknown\n"
        "    knowledge_state: inferred\n"
        "    classification_confidence: low\n"
        "\n"
        "Operators promote stubs by reviewing the archived original, copying the body\n"
        "into the new record, then enacting a category change via the configured\n"
        "`accountabilities.authority_channel`.\n"
        "\n"
        "## Migrated files\n"
        "\n"
    )
    if migrated:
        body += "\n".join(f"- {name}" for name in sorted(migrated)) + "\n"
    else:
        body += "(none — directory was empty at migration time)\n"
    return body


def scaffold_entities(
    instance_dir: Path,
    *,
    migrate_people: bool = False,
    templates_dir: Path | None = None,
) -> None:
    """Scaffold the relational awareness layer into instance_dir.

    Copies the three entities templates into `memory/L2/entities/`. When
    `migrate_people=True`, archives the existing `memory/L2/people/` directory
    (one-shot — archive existence is the marker) and generates a stub entity
    record per former people file with conservative defaults.

    Idempotent on the template copy: existing files are skipped with `[skip]`.
    Migration is one-shot — if the archive directory for today already exists,
    no further people-file moves happen.
    """
    templates = templates_dir if templates_dir is not None else _DEFAULT_TEMPLATES_DIR
    entities_src = templates / _ENTITIES_DIR

    for name in _ENTITY_TEMPLATE_FILES:
        src = entities_src / name
        if not src.exists():
            raise FileNotFoundError(f"template missing: {src}")

    entities_dst = instance_dir / _ENTITIES_DIR
    entities_dst.mkdir(parents=True, exist_ok=True)

    for name in _ENTITY_TEMPLATE_FILES:
        _copy_if_missing(entities_src / name, entities_dst / name)

    if not migrate_people:
        return

    today = _today_iso()
    archive_dir = instance_dir / _ARCHIVE_PARENT / f"people-pre-{today}"
    if archive_dir.exists():
        print(f"[skip] migration already ran — {archive_dir} exists")
        return

    people_dir = instance_dir / _PEOPLE_DIR
    if not people_dir.exists():
        print(f"[skip] no {_PEOPLE_DIR}/ directory to migrate")
        return

    people_files = sorted(p for p in people_dir.glob("*.md") if p.is_file())
    archive_dir.mkdir(parents=True, exist_ok=True)
    migrated: list[str] = []

    for src_path in people_files:
        dst_path = archive_dir / src_path.name
        src_path.rename(dst_path)
        migrated.append(src_path.name)
        print(f"[archive] {src_path.relative_to(instance_dir)} → "
              f"{dst_path.relative_to(instance_dir)}")

        slug = _entity_slug_from_path(src_path)
        stub_path = entities_dst / f"{slug}.md"
        if stub_path.exists():
            print(f"[skip] {stub_path.relative_to(instance_dir)} already exists")
            continue
        stub_path.write_text(_entity_stub(slug, today), encoding="utf-8")
        print(f"[write] {stub_path.relative_to(instance_dir)}")

    readme_path = archive_dir / "_README.md"
    readme_path.write_text(_archive_readme(today, migrated), encoding="utf-8")
    print(f"[write] {readme_path.relative_to(instance_dir)}")


def _patch_claude_md(instance_dir: Path, import_line: str, anchor: str) -> None:
    """Idempotently insert ``import_line`` above ``anchor`` in CLAUDE.md.

    If CLAUDE.md is absent, prints a warning and returns. If the import line
    is already present, prints `[skip]` and returns. If the anchor is missing,
    appends the import to the imports block (after the last `@memory/L1/...`
    line) and prints `[append]`.
    """
    claude_md = instance_dir / "CLAUDE.md"
    if not claude_md.exists():
        print(f"[warn] {claude_md} not found — skipping CLAUDE.md patch")
        return

    text = claude_md.read_text(encoding="utf-8")
    if import_line in text:
        print(f"[skip] CLAUDE.md already imports {import_line.strip()}")
        return

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    inserted = False
    for line in lines:
        if not inserted and line.strip() == anchor.strip():
            out.append(import_line if import_line.endswith("\n") else import_line + "\n")
            inserted = True
        out.append(line)

    if not inserted:
        last_import_idx = -1
        for i, line in enumerate(lines):
            if line.startswith("@memory/L1/"):
                last_import_idx = i
        if last_import_idx >= 0:
            insert_at = last_import_idx + 1
            patched = (
                lines[:insert_at]
                + [import_line if import_line.endswith("\n") else import_line + "\n"]
                + lines[insert_at:]
            )
            claude_md.write_text("".join(patched), encoding="utf-8")
            print(f"[append] CLAUDE.md ← {import_line.strip()}")
            return
        print("[warn] CLAUDE.md has no @memory/L1/* imports — cannot patch")
        return

    claude_md.write_text("".join(out), encoding="utf-8")
    print(f"[patch] CLAUDE.md ← {import_line.strip()}")


def scaffold_inter_agent(
    instance_dir: Path,
    templates_dir: Path | None = None,
) -> None:
    """Scaffold the inter-agent protocol into instance_dir.

    Copies the authority-map template into ``memory/L1/``, patches CLAUDE.md to
    import it between accountabilities-manifest and HOT, and prints the
    constitutional snippet for manual paste into ``memory/L1/RULES.md``.

    Idempotent: existing files / existing CLAUDE.md import lines are skipped.
    """
    templates = templates_dir if templates_dir is not None else _DEFAULT_TEMPLATES_DIR
    map_src = templates / _AUTHORITY_MAP_SRC
    snippet_src = templates / _INTER_AGENT_SNIPPET_SRC
    for src in (map_src, snippet_src):
        if not src.exists():
            raise FileNotFoundError(f"template missing: {src}")

    _copy_if_missing(map_src, instance_dir / _AUTHORITY_MAP_DST)

    _patch_claude_md(
        instance_dir,
        import_line="@memory/L1/authority-map.md\n",
        anchor="@memory/L1/HOT.md",
    )

    print()
    print("--- BEGIN RULES.md snippet ---")
    print(snippet_src.read_text(encoding="utf-8"), end="")
    print("--- END RULES.md snippet ---")
    print()
    print(
        "Paste the inter-agent protocol snippet into your memory/L1/RULES.md "
        "under your next free §-number."
    )

def scaffold_adaptive_discovery(
    instance_dir: Path,
    templates_dir: Path | None = None,
) -> None:
    """Print the adaptive-discovery constitutional snippet for paste into RULES.md.

    No file copies — the discipline has no L1/L2 dedicated file beyond the
    entity records owned by the relational awareness layer. Running twice is
    fine; the operator owns `RULES.md`.
    """
    templates = templates_dir if templates_dir is not None else _DEFAULT_TEMPLATES_DIR
    snippet_src = templates / _ADAPTIVE_DISCOVERY_SNIPPET_SRC
    if not snippet_src.exists():
        raise FileNotFoundError(f"template missing: {snippet_src}")

    # Touch instance_dir to keep the signature meaningful — surfaces a clear
    # error if the operator pointed at a non-existent path.
    if not instance_dir.exists():
        raise FileNotFoundError(f"instance_dir missing: {instance_dir}")

    print("--- BEGIN RULES.md snippet ---")
    print(snippet_src.read_text(encoding="utf-8"), end="")
    print("--- END RULES.md snippet ---")
    print()
    print(
        "Paste the adaptive discovery snippet into your memory/L1/RULES.md "
        "under your next free §-number."
    )

