"""Claude brain wrapper.

Relies on Claude Code's own auto-loaded `CLAUDE.md` rather than the gateway
preamble, so we set `needs_l1_preamble = False`. Resume id is captured by
finding the most recent `.jsonl` session file in `~/.claude/projects/<slug>/`
modified at or after the adapter start time.
"""

from __future__ import annotations

from pathlib import Path

from ..context import (
    render_accountabilities_manifest_block,
    render_adaptive_discovery_block,
    render_authority_block,
    render_authority_map_block,
    render_clock_inline,
    render_entities_block,
    render_voice_anchor,
)
from ..queue import Event
from .base import Brain, newest_jsonl_stem, parse_iso


class ClaudeBrain(Brain):
    name = "claude"
    needs_l1_preamble = False
    goal_delivery = "system_prompt"  # JC_GOAL → claude.sh --append-system-prompt

    def capture_session_id(self, started_at: str) -> str | None:
        t0 = parse_iso(started_at)
        if t0 is None:
            return None
        slug = str(self.instance_dir).replace("/", "-").replace("_", "-")
        return newest_jsonl_stem(Path.home() / ".claude" / "projects" / slug, t0)

    def _user_message_body(self, event: Event) -> str:
        # Claude auto-loads CLAUDE.md and resumes via session id, so we
        # cannot inject the dynamic clock into the preamble. Prefix the
        # user message with a single-line clock so each turn sees fresh
        # "now" without polluting the cached CLAUDE.md view.
        clock_line = render_clock_inline(self._timezone())
        anchor = render_voice_anchor(self.instance_dir)
        manifest_block = render_accountabilities_manifest_block(self.instance_dir)
        authority_block = render_authority_block(self.instance_dir)
        entities_block = render_entities_block(self.instance_dir)
        authority_map_block = render_authority_map_block(self.instance_dir)
        adaptive_block = render_adaptive_discovery_block(self.instance_dir)
        body = event.content or ""
        parts = [clock_line]
        if anchor:
            parts.append(f"[Voice: {anchor}]")
        if manifest_block:
            parts.append(manifest_block)
        if entities_block:
            parts.append(entities_block)
        if authority_map_block:
            parts.append(authority_map_block)
        if adaptive_block:
            parts.append(adaptive_block)
        if authority_block:
            parts.append(authority_block)
        if body:
            parts.append(body)
        return "\n".join(parts)
