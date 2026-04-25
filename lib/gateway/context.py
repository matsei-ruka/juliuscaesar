"""Context loader for non-Claude brains.

Claude Code auto-loads `CLAUDE.md` from the working directory. Other native
CLIs do not, so the gateway concatenates the L1 memory files and prepends them
as a system preamble inside the prompt.

The loader caches its rendered output per-instance, keyed by the highest mtime
across the L1 directory, so we do not re-read on every event.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path


_CACHE: dict[Path, "_CachedPreamble"] = {}
_CACHE_LOCK = threading.Lock()

L1_FILES = ("IDENTITY.md", "USER.md", "RULES.md", "HOT.md")
MAX_BYTES_PER_FILE = 8000


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
    """Return concatenated L1 memory as a system-prompt preamble."""

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
    text = (
        "You are Julius, the assistant for this JuliusCaesar instance. "
        "Use the layered memory below to inform every reply.\n\n# Instance memory\n\n"
        + ("\n\n".join(sections) if sections else "(No L1 memory files found.)")
    )
    with _CACHE_LOCK:
        _CACHE[instance_dir] = _CachedPreamble(text=text, fingerprint=fingerprint)
    return text


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
