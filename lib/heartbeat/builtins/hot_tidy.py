"""hot_tidy — enforce HOT.md size + section limits, archive overflow to L2.

Sections recognized (case-insensitive prefix match on the H2 title):

  ``what shipped``      → archive overflow to ``memory/L2/completed/<slug>.md``
  ``open threads``      → archive overflow to ``memory/L2/projects/<slug>.md``
  ``known nuisances``   → archive overflow to ``memory/L2/learnings/<slug>.md``

Each section's items can be either bulletpoints (lines starting with ``- `` or
``* ``) or blank-line-separated paragraphs (e.g. bolded leads like
``**PR #26 …** Direct OpenAI…``). Both shapes are preserved on rewrite.

Caps: ``MAX_ITEMS_PER_SECTION = 5``. The newest items are kept (top of section
by file order). Older overflow items are written as L2 entries with frontmatter
and the original markdown body, then removed from HOT.md.

The task is opt-in: it ships disabled in tasks.yaml; operators enable by
adding a task block with ``builtin: hot_tidy``.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path


HOT_MD_PATH = Path("memory/L1/HOT.md")
L2_ROOT = Path("memory/L2")

MAX_ITEMS_PER_SECTION = 5
MAX_TOTAL_LINES = 400
MAX_WORDS_PER_ITEM = 100


# Section title prefix → (canonical key, L2 destination subdir)
_SECTION_RULES: tuple[tuple[str, str, str], ...] = (
    ("what shipped", "what_shipped", "completed"),
    ("open threads", "open_threads", "projects"),
    ("immediate open threads", "open_threads", "projects"),
    ("known nuisances", "known_nuisances", "learnings"),
)


# A single item inside a section. ``shape`` records whether the item came in
# as a bullet (``"bullet"``) or a paragraph block (``"paragraph"``) so we can
# reserialize without mangling the file.
@dataclass
class Section:
    title: str  # Original H2 title (sans "## ")
    canonical: str | None  # canonical key from _SECTION_RULES, else None
    l2_subdir: str | None  # destination under memory/L2/, else None
    shape: str = "bullet"  # "bullet" | "paragraph" | "empty"
    items: list[str] = field(default_factory=list)
    leading: list[str] = field(default_factory=list)  # blank/preamble lines after header


@dataclass
class ParsedHotMd:
    frontmatter: list[str]  # raw frontmatter lines incl. delimiters; [] if none
    preamble: list[str]  # everything before first H2 (incl. H1 + intro text)
    sections: list[Section]
    trailing: list[str] = field(default_factory=list)


# --- Parsing -----------------------------------------------------------------


def _classify(title: str) -> tuple[str | None, str | None]:
    """Map an H2 title to a canonical section key + L2 subdir."""
    lo = title.strip().lower()
    for prefix, canonical, subdir in _SECTION_RULES:
        if lo.startswith(prefix):
            return canonical, subdir
    return None, None


def _parse_frontmatter(lines: list[str]) -> tuple[list[str], int]:
    if not lines or lines[0].strip() != "---":
        return [], 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[: i + 1], i + 1
    return [], 0


def parse(text: str) -> ParsedHotMd:
    lines = text.splitlines()
    frontmatter, idx = _parse_frontmatter(lines)
    preamble: list[str] = []
    while idx < len(lines) and not lines[idx].startswith("## "):
        preamble.append(lines[idx])
        idx += 1

    sections: list[Section] = []
    while idx < len(lines):
        line = lines[idx]
        if not line.startswith("## "):
            # Trailing content after sections is rare; collect into the last
            # section as plain trailing lines.
            if sections:
                sections[-1].leading.append(line)
            idx += 1
            continue
        title = line[3:].strip()
        canonical, subdir = _classify(title)
        section = Section(title=title, canonical=canonical, l2_subdir=subdir)
        idx += 1
        # Collect leading blank lines after header
        body_lines: list[str] = []
        while idx < len(lines) and not lines[idx].startswith("## "):
            body_lines.append(lines[idx])
            idx += 1
        section.shape, section.leading, section.items = _split_items(body_lines)
        sections.append(section)

    return ParsedHotMd(frontmatter=frontmatter, preamble=preamble, sections=sections)


_BULLET_RE = re.compile(r"^\s*[-*]\s+")


def _split_items(lines: list[str]) -> tuple[str, list[str], list[str]]:
    """Split a section body into (shape, leading_blanks, items).

    ``leading_blanks`` are the empty lines between the H2 and the first content.
    Items either start with a bullet (``- ``/``* ``) or are blocks separated
    by one or more blank lines (paragraph mode).
    """
    leading: list[str] = []
    i = 0
    while i < len(lines) and not lines[i].strip():
        leading.append(lines[i])
        i += 1
    rest = lines[i:]
    if not rest:
        return "empty", leading, []
    # Decide shape from the first non-blank line.
    if _BULLET_RE.match(rest[0]):
        return "bullet", leading, _split_bullet_items(rest)
    return "paragraph", leading, _split_paragraph_items(rest)


def _split_bullet_items(lines: list[str]) -> list[str]:
    """Group lines into items where each item starts at a bullet line.

    Continuation lines (indented or non-bullet, non-blank) attach to the
    preceding bullet so wrapped descriptions stay intact.
    """
    items: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            # Drop trailing blank lines from each item so reserialization is tidy.
            while current and not current[-1].strip():
                current.pop()
            if current:
                items.append("\n".join(current))
            current.clear()

    for line in lines:
        if _BULLET_RE.match(line):
            flush()
            current.append(line)
        elif line.strip() == "":
            current.append(line)
        else:
            current.append(line)
    flush()
    return items


def _split_paragraph_items(lines: list[str]) -> list[str]:
    """Group lines into items separated by one or more blank lines."""
    items: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.strip() == "":
            if current:
                items.append("\n".join(current).rstrip())
                current = []
        else:
            current.append(line)
    if current:
        items.append("\n".join(current).rstrip())
    return [it for it in items if it.strip()]


# --- Slug + L2 archive -------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug_for(item: str, *, prefix: str = "") -> str:
    """Derive a stable slug from the first line of an item."""
    first = item.strip().splitlines()[0] if item.strip() else "item"
    # Strip leading bullet markers and bold delimiters.
    cleaned = re.sub(r"^[-*]\s+", "", first)
    cleaned = cleaned.replace("**", "").replace("__", "")
    cleaned = cleaned.lower()
    cleaned = _SLUG_RE.sub("-", cleaned).strip("-")
    cleaned = cleaned[:60].rstrip("-")
    if not cleaned:
        cleaned = "item"
    return f"{prefix}{cleaned}"


def _item_word_count(item: str) -> int:
    return len(item.split())


def _frontmatter_for(canonical: str, slug: str, item: str) -> str:
    today = dt.date.today().isoformat()
    title = item.strip().splitlines()[0]
    title = re.sub(r"^[-*]\s+", "", title).replace("**", "").strip()
    if len(title) > 80:
        title = title[:77] + "..."
    type_field = {
        "what_shipped": "completed",
        "open_threads": "project",
        "known_nuisances": "learning",
    }.get(canonical, "note")
    fm = [
        "---",
        f"slug: {slug}",
        f"title: {title}",
        "layer: L2",
        f"type: {type_field}",
        "state: archived",
        f"created: {today}",
        f"updated: {today}",
        f"archived_from: HOT.md/{canonical}",
        "tags: [hot-archive]",
        "links: []",
        "---",
        "",
    ]
    return "\n".join(fm)


def _write_l2_archive(
    instance_dir: Path,
    *,
    canonical: str,
    subdir: str,
    item: str,
    dry_run: bool,
) -> tuple[Path, str]:
    slug_base = _slug_for(item)
    slug = f"{subdir}/{slug_base}"
    target = instance_dir / L2_ROOT / subdir / f"{slug_base}.md"
    counter = 2
    while target.exists():
        slug = f"{subdir}/{slug_base}-{counter}"
        target = instance_dir / L2_ROOT / subdir / f"{slug_base}-{counter}.md"
        counter += 1
    if dry_run:
        return target, slug
    target.parent.mkdir(parents=True, exist_ok=True)
    body = _frontmatter_for(canonical, slug, item) + item.strip() + "\n"
    target.write_text(body, encoding="utf-8")
    return target, slug


# --- Rewrite -----------------------------------------------------------------


def render(parsed: ParsedHotMd) -> str:
    out: list[str] = []
    if parsed.frontmatter:
        out.extend(parsed.frontmatter)
    if parsed.preamble:
        # Update header date if a "(today YYYY-MM-DD)" / "(rolling …)" stamp is
        # present on the H1; otherwise leave preamble alone.
        parsed.preamble = _stamp_today(parsed.preamble)
        out.extend(parsed.preamble)
    for section in parsed.sections:
        out.append(f"## {section.title}")
        out.extend(section.leading or [""])
        if section.shape == "bullet":
            for idx, item in enumerate(section.items):
                out.append(item)
                if idx != len(section.items) - 1:
                    out.append("")
        elif section.shape == "paragraph":
            for idx, item in enumerate(section.items):
                out.append(item)
                if idx != len(section.items) - 1:
                    out.append("")
        # empty: no items, just leading
        out.append("")
    text = "\n".join(out)
    # Collapse runs of >2 blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text.endswith("\n"):
        text += "\n"
    return text


_TODAY_STAMP_RE = re.compile(r"\(today\s+\d{4}-\d{2}-\d{2}\)")


def _stamp_today(preamble: list[str]) -> list[str]:
    today = dt.date.today().isoformat()
    out = []
    stamped = False
    for line in preamble:
        if not stamped and line.startswith("# "):
            if _TODAY_STAMP_RE.search(line):
                line = _TODAY_STAMP_RE.sub(f"(today {today})", line)
                stamped = True
        out.append(line)
    return out


# --- Top-level run -----------------------------------------------------------


def run(instance_dir: Path, dry_run: bool = False) -> dict:
    """Tidy HOT.md. Returns a summary dict (counts, archived paths)."""
    hot_path = instance_dir / HOT_MD_PATH
    if not hot_path.exists():
        return {
            "ok": False,
            "reason": f"HOT.md not found at {hot_path}",
            "sections": {},
            "archived": [],
        }

    text = hot_path.read_text(encoding="utf-8")
    parsed = parse(text)

    summary: dict = {
        "ok": True,
        "dry_run": dry_run,
        "path": str(hot_path),
        "sections": {},
        "archived": [],
        "warnings": [],
    }

    for section in parsed.sections:
        before = len(section.items)
        if section.canonical and before > MAX_ITEMS_PER_SECTION:
            keep = section.items[:MAX_ITEMS_PER_SECTION]
            overflow = section.items[MAX_ITEMS_PER_SECTION:]
            for item in overflow:
                target, slug = _write_l2_archive(
                    instance_dir,
                    canonical=section.canonical,
                    subdir=section.l2_subdir or "completed",
                    item=item,
                    dry_run=dry_run,
                )
                summary["archived"].append(
                    {
                        "section": section.canonical,
                        "slug": slug,
                        "path": str(target),
                    }
                )
            section.items = keep

        summary["sections"][section.canonical or section.title] = {
            "before": before,
            "after": len(section.items),
            "max": MAX_ITEMS_PER_SECTION if section.canonical else None,
        }

        for item in section.items:
            wc = _item_word_count(item)
            if wc > MAX_WORDS_PER_ITEM:
                summary["warnings"].append(
                    f"section={section.canonical or section.title} "
                    f"item exceeds {MAX_WORDS_PER_ITEM} words ({wc})"
                )

    new_text = render(parsed)
    line_count = new_text.count("\n")
    summary["lines"] = line_count
    if line_count > MAX_TOTAL_LINES:
        summary["warnings"].append(
            f"HOT.md is {line_count} lines (max {MAX_TOTAL_LINES}); "
            "consider trimming sections manually"
        )

    if not dry_run and new_text != text:
        hot_path.write_text(new_text, encoding="utf-8")
        summary["written"] = True
    else:
        summary["written"] = False

    return summary
