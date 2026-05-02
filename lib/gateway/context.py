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

import threading
from dataclasses import dataclass
from pathlib import Path


_CACHE: dict[Path, "_CachedPreamble"] = {}
_CACHE_LOCK = threading.Lock()

L1_FILES = ("IDENTITY.md", "USER.md", "RULES.md", "HOT.md", "CHATS.md")
MAX_BYTES_PER_FILE = 8000


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
    text = "\n\n".join(
        [
            _ROLE_PREAMBLE,
            "# Instance memory",
            memory_block,
            _L2_GUIDANCE,
            _FRAMEWORK_HINTS,
            _CAVEMAN,
        ]
    )
    with _CACHE_LOCK:
        _CACHE[instance_dir] = _CachedPreamble(text=text, fingerprint=fingerprint)
    return text


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
