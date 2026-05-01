"""journal_tidy — sweep aged JOURNAL.md entries based on their State field.

Entries with timestamp older than ROLLING_DAYS and a terminal State are
archived to ``memory/L2/journal-archive/YYYY-MM.md`` and removed from the
journal. Entries in non-terminal states (`open`, `under-test`) stay
regardless of age.

Terminal states:
  - ``resolved``        → archive
  - ``abandoned``       → archive (with the entry's own abandonment note)
  - ``promoted-to-L2``  → drop (operator has already migrated content;
                          archiving would duplicate)

Non-terminal states:
  - ``open``            → keep
  - ``under-test``      → keep
  - missing State       → keep (warn in summary)

The sweeper is opt-in: operators enable it by adding a task block in
``tasks.yaml`` with ``builtin: journal_tidy`` and ``enabled: true``.
Recommended cadence: daily.

Heading format expected (per `journal-preamble-en.md` entry schema):

    ## YYYY-MM-DD HH:MM — <slug>
    **Trigger:** ...
    **Context:** ...
    **Observation:** ...
    **Pattern hypothesis:** ...
    **Test/next action:** ...
    **State:** [open | under-test | resolved | promoted-to-L2 | abandoned]
    **Update log:**
    - YYYY-MM-DD HH:MM — ...

For Italian-language journals (e.g. Mario), the State line is matched by
either ``**State:**`` or ``**Stato:**`` to keep the builtin source-language-
agnostic.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path


JOURNAL_PATH = Path("memory/L1/JOURNAL.md")
ARCHIVE_DIR = Path("memory/L2/journal-archive")

ROLLING_DAYS = 30

TERMINAL_STATES_ARCHIVE = {"resolved", "abandoned"}
TERMINAL_STATES_DROP = {"promoted-to-l2", "promoted_to_l2"}
NON_TERMINAL_STATES = {"open", "under-test", "under_test"}


# Heading captures the entry timestamp.
_HEADING_RE = re.compile(
    r"^## (\d{4}-\d{2}-\d{2})(?:\s+\d{2}:\d{2})?\s*[—–-]?\s*(.*)$",
    re.MULTILINE,
)

# State line — matches both English and Italian.
_STATE_RE = re.compile(
    r"^\*\*(?:State|Stato):\*\*\s*\[?\s*([a-zA-Z][a-zA-Z0-9 _-]+?)\s*[\]\n|]",
    re.MULTILINE,
)


@dataclass
class Entry:
    heading: str           # full heading line (without trailing newline)
    body: str              # everything until the next H2 or EOF
    date: dt.date | None   # parsed from heading; None if unparseable
    slug: str              # heading text after the date
    state: str             # canonical lowercase state, "" if not found


def run(instance_dir: Path, dry_run: bool = False) -> dict:
    """Sweep aged journal entries; archive or drop the terminal ones."""
    journal = instance_dir / JOURNAL_PATH
    if not journal.exists():
        return {"ok": True, "skipped": "JOURNAL.md not found"}

    text = journal.read_text(encoding="utf-8")
    preamble, entries = _split_journal(text)

    if not entries:
        return {"ok": True, "swept": 0, "kept": 0, "note": "no entries"}

    today = dt.date.today()
    cutoff = today - dt.timedelta(days=ROLLING_DAYS)

    archived: list[Entry] = []
    dropped: list[Entry] = []
    kept: list[Entry] = []
    no_state: list[Entry] = []

    for entry in entries:
        if entry.state == "":
            no_state.append(entry)
            kept.append(entry)
            continue
        if entry.date is None or entry.date >= cutoff:
            kept.append(entry)
            continue
        if entry.state in TERMINAL_STATES_ARCHIVE:
            archived.append(entry)
        elif entry.state in TERMINAL_STATES_DROP:
            dropped.append(entry)
        else:
            # Non-terminal or unknown — keep.
            kept.append(entry)

    if dry_run:
        return _summary(archived, dropped, kept, no_state, dry_run=True)

    # Append archived entries to per-month archive files.
    if archived:
        archive_root = instance_dir / ARCHIVE_DIR
        archive_root.mkdir(parents=True, exist_ok=True)
        by_month: dict[str, list[Entry]] = {}
        for entry in archived:
            assert entry.date is not None
            key = entry.date.strftime("%Y-%m")
            by_month.setdefault(key, []).append(entry)
        for month, items in by_month.items():
            target = archive_root / f"{month}.md"
            existing = target.read_text(encoding="utf-8") if target.exists() else (
                f"# Journal archive — {month}\n\n"
                "Archived journal entries (`resolved` or `abandoned` state, "
                f"swept after {ROLLING_DAYS}-day rolling window).\n\n"
            )
            blocks = "\n\n".join(_render_entry(e) for e in items) + "\n"
            target.write_text(existing.rstrip() + "\n\n" + blocks, encoding="utf-8")

    # Rewrite the journal without archived/dropped entries.
    new_text = preamble + "\n\n".join(_render_entry(e) for e in kept)
    if not new_text.endswith("\n"):
        new_text += "\n"
    journal.write_text(new_text, encoding="utf-8")

    return _summary(archived, dropped, kept, no_state, dry_run=False)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _split_journal(text: str) -> tuple[str, list[Entry]]:
    """Split text at the first '## YYYY-MM-DD' heading; everything before is preamble."""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return text, []

    preamble = text[: matches[0].start()]

    entries: list[Entry] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        heading = m.group(0).rstrip("\n")
        body = text[m.end():end]
        date_str = m.group(1)
        slug = m.group(2).strip()
        try:
            date = dt.date.fromisoformat(date_str)
        except ValueError:
            date = None
        state = _extract_state(body)
        entries.append(Entry(
            heading=heading,
            body=body,
            date=date,
            slug=slug,
            state=state,
        ))
    return preamble, entries


def _extract_state(body: str) -> str:
    """Find `**State:** ...` or `**Stato:** ...` line and return canonical lowercase."""
    m = _STATE_RE.search(body)
    if not m:
        return ""
    raw = m.group(1).strip().lower()
    # Canonicalize underscores/hyphens.
    return raw.replace(" ", "-").replace("_", "-")


def _render_entry(entry: Entry) -> str:
    """Reserialize an entry to its markdown form."""
    return entry.heading + "\n" + entry.body.rstrip()


def _summary(
    archived: list[Entry],
    dropped: list[Entry],
    kept: list[Entry],
    no_state: list[Entry],
    *,
    dry_run: bool,
) -> dict:
    return {
        "ok": True,
        "dry_run": dry_run,
        "rolling_days": ROLLING_DAYS,
        "archived": [e.slug for e in archived],
        "dropped": [e.slug for e in dropped],
        "kept": len(kept),
        "no_state_warnings": [e.slug for e in no_state],
    }
