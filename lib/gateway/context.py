"""Context loader for non-Claude brains.

Claude Code auto-loads `CLAUDE.md` from the working directory. Other native
CLIs do not, so the gateway concatenates the L1 memory files and prepends them
as a system preamble inside the prompt.

Per docs/specs/codex-main-brain-hardening.md §Phase 2, the preamble is
semantically equivalent to `CLAUDE.md`: instance role, expanded L1 memory,
L2 retrieval guidance, framework command hints, and token-efficiency rules.

The loader caches its rendered output per-instance, keyed by the highest mtime
across the L1 directory, so we do not re-read on every event.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


_CACHE: dict[Path, "_CachedPreamble"] = {}
_CACHE_LOCK = threading.Lock()

L1_FILES = ("IDENTITY.md", "STYLE.md", "USER.md", "RULES.md", "HOT.md", "CHATS.md")
MAX_BYTES_PER_FILE = 8000
_VOICE_ANCHOR_LINE_RE = re.compile(r"^>\s*(.+)$", re.MULTILINE)
_SECTION_RE_TEMPLATE = r"^#{{1,6}}\s+{heading}\s*$"


_ROLE_PREAMBLE = (
    "You are Julius, the assistant for this JuliusCaesar instance. "
    "You are running as a gateway chat brain, not as an autonomous worker. "
    "Answer the user; do not narrate gateway metadata. "
    "Do not edit files unless the user explicitly asks for coding or "
    "maintenance work. Use the layered memory below to inform every reply."
)

_L2_GUIDANCE = """# How to use more context

The L2 knowledge base lives at `memory/L2/` and is searchable with `jc memory`:

```
jc memory search "<query>"   # FTS5 search across L1 + L2
jc memory read <slug>        # full entry body + backlinks
```

Conversation transcripts live at `state/transcripts/<conversation_id>.jsonl`:

```
jc transcripts read <conversation_id>
jc transcripts tail <conversation_id> [--lines N]
jc transcripts search "<query>" [--user X] [--since YYYY-MM-DD]
jc transcripts get <message_id>
```

Use these instead of guessing when the user references the past."""

_FRAMEWORK_HINTS = """# Framework commands

- `jc doctor` — diagnostics
- `jc memory search "<query>"` / `jc memory read <slug>`
- `jc workers list` / `jc heartbeat run <task>` / `jc watchdog status`"""

_CAVEMAN = """# Token efficiency (caveman mode)

Respond terse like smart caveman. All technical substance stay. Only fluff die.
Drop articles, filler, pleasantries, hedging. Fragments OK. Code blocks unchanged.
Default level: full. Switch with `/caveman lite|full|ultra`. Persist until changed.

Auto-clarity: drop caveman for security warnings, irreversible action
confirmations, multi-step sequences where fragment order risks misread.
Resume after."""


@dataclass(frozen=True)
class _CachedPreamble:
    text: str
    fingerprint: float


def _l1_dir(instance_dir: Path) -> Path:
    return instance_dir / "memory" / "L1"


def _fingerprint(l1_dir: Path) -> float:
    if not l1_dir.is_dir():
        return 0.0
    latest = 0.0
    for name in L1_FILES:
        path = l1_dir / name
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    return latest


def _style_path(instance_dir: Path) -> Path:
    return _l1_dir(instance_dir) / "STYLE.md"


def _extract_section(text: str, heading: str) -> str:
    pattern = re.compile(
        _SECTION_RE_TEMPLATE.format(heading=re.escape(heading)),
        re.MULTILINE | re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"^#{1,6}\s+", text[start:], re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(text)
    return text[start:end].strip()


def render_voice_anchor(instance_dir: Path) -> str:
    """Return STYLE.md's one-line voice anchor, or "" when unavailable."""

    path = _style_path(instance_dir)
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    section = _extract_section(text, "Voice anchor")
    if not section:
        return ""
    matches = _VOICE_ANCHOR_LINE_RE.findall(section)
    if not matches:
        return ""
    anchor = matches[-1].strip()
    return anchor if len(anchor) <= 300 else ""


def caveman_enabled(instance_dir: Path) -> bool:
    """STYLE.md controls whether framework caveman guidance is injected.

    Missing STYLE.md or missing flag keeps caveman off. It is opt-in.
    """

    path = _style_path(instance_dir)
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    section = _extract_section(text, "Caveman")
    search_text = section or text
    match = re.search(r"^caveman:\s*(\w+)", search_text, re.MULTILINE | re.IGNORECASE)
    if not match:
        return False
    return match.group(1).lower() == "enabled"


def render_preamble(instance_dir: Path) -> str:
    """Return concatenated L1 memory + L2/framework/caveman guidance."""

    l1_dir = _l1_dir(instance_dir)
    fingerprint = _fingerprint(l1_dir)
    with _CACHE_LOCK:
        cached = _CACHE.get(instance_dir)
        if cached is not None and cached.fingerprint == fingerprint:
            return cached.text

    sections: list[str] = []
    for name in L1_FILES:
        path = l1_dir / name
        if not path.exists():
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sections.append(f"## {name}\n{body[:MAX_BYTES_PER_FILE]}")
    memory_block = "\n\n".join(sections) if sections else "(No L1 memory files found.)"
    parts = [
        _ROLE_PREAMBLE,
        "# Instance memory",
        memory_block,
        _L2_GUIDANCE,
        _FRAMEWORK_HINTS,
    ]
    if caveman_enabled(instance_dir):
        parts.append(_CAVEMAN)
    text = "\n\n".join(parts)
    with _CACHE_LOCK:
        _CACHE[instance_dir] = _CachedPreamble(text=text, fingerprint=fingerprint)
    return text


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def render_clock(tz_name: str) -> str:
    """Return a fresh clock block for the configured timezone.

    Evaluated each call — must NOT be cached. Brain prompts inject this so
    the LLM reasons about "now" in the user's local zone, not UTC.
    """

    name = (tz_name or "UTC").strip() or "UTC"
    now = datetime.now(ZoneInfo(name))
    iso = now.isoformat(timespec="seconds")
    raw_offset = now.strftime("%z")  # e.g. "+0400" or "-0500"
    if raw_offset:
        pretty_offset = f"UTC{raw_offset[:3]}:{raw_offset[3:]}"
    else:
        pretty_offset = "UTC"
    return (
        "# Current time\n"
        f"{now.strftime('%Y-%m-%d %H:%M')} {name} "
        f"({pretty_offset}, ISO 8601: {iso})"
    )


def render_clock_inline(tz_name: str) -> str:
    """One-line clock prefix for brains that auto-load CLAUDE.md.

    Compact form: `[Current time: 2026-05-08 18:30 Asia/Dubai (UTC+04:00)]`.
    """

    name = (tz_name or "UTC").strip() or "UTC"
    now = datetime.now(ZoneInfo(name))
    raw_offset = now.strftime("%z")
    pretty_offset = f"UTC{raw_offset[:3]}:{raw_offset[3:]}" if raw_offset else "UTC"
    return f"[Current time: {now.strftime('%Y-%m-%d %H:%M')} {name} ({pretty_offset})]"
