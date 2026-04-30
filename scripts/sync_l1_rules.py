#!/usr/bin/env python3
"""Sync canonical L1 RULES.md sections into an existing instance.

Templates under ``templates/init-instance/memory/L1/RULES.md`` only
apply when a new instance is created. Existing instances need an
explicit migration to pick up new standing-rule sections that the
framework adds over time (e.g. "Conversation transcripts", "HOT.md
structure").

This script appends any **missing** H2 sections from the framework
template to the instance's ``memory/L1/RULES.md``. Sections already
present (matched by exact heading text) are left alone — the script
is idempotent and safe to re-run.

Default sync set (override via repeated ``--section``):

  - "## Conversation transcripts"
  - "## HOT.md structure"

Usage:

  scripts/sync_l1_rules.py --instance-dir /path/to/instance [--dry-run]
  scripts/sync_l1_rules.py --instance-dir ... --section "## Foo" --section "## Bar"

The script never overwrites or rewrites the existing file. It only
appends missing sections, in template order, separated by a blank
line.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_FRAMEWORK = _HERE.parent
_TEMPLATE_RULES = (
    _FRAMEWORK / "templates" / "init-instance" / "memory" / "L1" / "RULES.md"
)

DEFAULT_SECTIONS = [
    "## Conversation transcripts",
    "## HOT.md structure",
]


def _extract_section(template_text: str, heading: str) -> str | None:
    """Return the body of an H2 section, including its heading line.

    Section runs from the matched heading up to (but excluding) the
    next ``## `` heading or end-of-file. Surrounding whitespace is
    stripped so callers control line spacing.
    """
    lines = template_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == heading:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## ") and lines[j].strip() != heading:
            end = j
            break
    return "\n".join(lines[start:end]).rstrip()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--instance-dir", required=True, type=Path)
    p.add_argument(
        "--section",
        action="append",
        default=None,
        help="H2 heading to sync (repeatable). Defaults to the framework set.",
    )
    p.add_argument(
        "--template",
        type=Path,
        default=_TEMPLATE_RULES,
        help="path to canonical RULES.md template",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    target = args.instance_dir / "memory" / "L1" / "RULES.md"
    if not target.is_file():
        sys.exit(f"target not found: {target}")
    if not args.template.is_file():
        sys.exit(f"template not found: {args.template}")

    sections = args.section or DEFAULT_SECTIONS
    template_text = args.template.read_text(encoding="utf-8")
    existing_text = target.read_text(encoding="utf-8")

    appended: list[tuple[str, str]] = []
    skipped: list[str] = []
    missing_in_template: list[str] = []

    for heading in sections:
        if heading in existing_text:
            skipped.append(heading)
            continue
        body = _extract_section(template_text, heading)
        if body is None:
            missing_in_template.append(heading)
            continue
        appended.append((heading, body))

    if not appended:
        if missing_in_template:
            for h in missing_in_template:
                print(f"warning: section not found in template: {h}", file=sys.stderr)
        print(f"(nothing to do; {len(skipped)} section(s) already present)")
        return 0

    insert = "\n\n" + "\n\n".join(body for _, body in appended) + "\n"
    if args.dry_run:
        for h, _ in appended:
            print(f"would append: {h}")
        for h in skipped:
            print(f"already present: {h}")
        for h in missing_in_template:
            print(f"missing in template: {h}", file=sys.stderr)
        print(f"(dry-run; {len(insert)} bytes would be appended to {target})")
        return 0

    new_text = existing_text.rstrip() + insert
    target.write_text(new_text, encoding="utf-8")
    for h, _ in appended:
        print(f"appended: {h}")
    for h in skipped:
        print(f"already present: {h}")
    for h in missing_in_template:
        print(f"warning: section not found in template: {h}", file=sys.stderr)
    print(f"updated: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
