"""Scaffold opt-in memory features into an instance directory.

Currently supports the accountability manifest scaffolding flow described in
docs/specs/accountabilities.md §Phase 3. Copies template files into the
operator's instance dir and prints the constitutional snippet to stdout for
manual paste into `memory/L1/RULES.md`.
"""

from __future__ import annotations

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

_CLAUDE_MD_IMPORT = "@memory/L1/accountabilities-manifest.md"
_CLAUDE_MD_ANCHOR = "@memory/L1/HOT.md"


def _copy_if_missing(src: Path, dst: Path) -> bool:
    if dst.exists():
        print(f"[skip] {dst} already exists — not overwriting")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"[write] {dst}")
    return True


def _patch_claude_md(instance_dir: Path) -> None:
    """Insert the accountabilities-manifest import into CLAUDE.md.

    Inserts before @memory/L1/HOT.md when found. Falls back to after the last
    @memory/L1/ import, or appends at end. Idempotent.
    """
    claude_md = instance_dir / "CLAUDE.md"
    if not claude_md.exists():
        print(
            f"[skip] CLAUDE.md not found — add {_CLAUDE_MD_IMPORT} manually "
            "before @memory/L1/HOT.md"
        )
        return

    text = claude_md.read_text(encoding="utf-8")

    if _CLAUDE_MD_IMPORT in text:
        print(f"[skip] CLAUDE.md already imports accountabilities-manifest.md")
        return

    if _CLAUDE_MD_ANCHOR in text:
        patched = text.replace(
            _CLAUDE_MD_ANCHOR,
            f"{_CLAUDE_MD_IMPORT}\n{_CLAUDE_MD_ANCHOR}",
            1,
        )
    else:
        lines = text.splitlines(keepends=True)
        last_import_idx = -1
        for i, line in enumerate(lines):
            if line.strip().startswith("@memory/L1/"):
                last_import_idx = i
        if last_import_idx >= 0:
            lines.insert(last_import_idx + 1, f"{_CLAUDE_MD_IMPORT}\n")
            patched = "".join(lines)
        else:
            patched = text.rstrip("\n") + f"\n{_CLAUDE_MD_IMPORT}\n"
            print(
                f"[warn] @memory/L1/HOT.md not found in CLAUDE.md — "
                f"appended {_CLAUDE_MD_IMPORT} at end"
            )

    claude_md.write_text(patched, encoding="utf-8")
    print(f"[write] CLAUDE.md — inserted {_CLAUDE_MD_IMPORT}")


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
    _patch_claude_md(instance_dir)
