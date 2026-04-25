"""Claude brain wrapper.

Relies on Claude Code's own auto-loaded `CLAUDE.md` rather than the gateway
preamble, so we set `needs_l1_preamble = False`. Resume id is captured by
finding the most recent `.jsonl` session file in `~/.claude/projects/<slug>/`
modified at or after the adapter start time.
"""

from __future__ import annotations

from pathlib import Path

from .base import Brain, newest_jsonl_stem, parse_iso


class ClaudeBrain(Brain):
    name = "claude"
    needs_l1_preamble = False

    def capture_session_id(self, started_at: str) -> str | None:
        t0 = parse_iso(started_at)
        if t0 is None:
            return None
        slug = str(self.instance_dir).replace("/", "-").replace("_", "-")
        return newest_jsonl_stem(Path.home() / ".claude" / "projects" / slug, t0)
