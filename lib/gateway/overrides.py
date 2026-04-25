"""Inline brain override parsing.

Two forms are supported:

  - Inline prefix: `[opus] explain X` → strips prefix, sets brain_override
  - Slash command: `/brain opus` (or `/brain claude:opus-4-7-1m`) → returns
    a system reply and a sticky-update intent. The runtime acks and updates
    the sticky table without invoking a brain.

Short names are resolved via `brains.aliases.resolve_alias`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .brains.aliases import resolve_alias


_INLINE_PREFIX_RE = re.compile(r"^\s*\[(?P<spec>[A-Za-z0-9_:.\-]+)\]\s*(?P<rest>.*)$", re.DOTALL)
_SLASH_BRAIN_RE = re.compile(r"^\s*/brain\s+(?P<spec>[A-Za-z0-9_:.\-]+)\s*$", re.IGNORECASE)
_SLASH_HELP_RE = re.compile(r"^\s*/brain\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class InlineOverride:
    spec: str
    cleaned_content: str


@dataclass(frozen=True)
class SlashCommand:
    kind: str
    spec: str | None = None
    reply: str | None = None


def parse_inline_override(content: str) -> InlineOverride | None:
    """Strip a leading `[brain]` prefix and return the canonical spec."""
    match = _INLINE_PREFIX_RE.match(content or "")
    if not match:
        return None
    raw_spec = match.group("spec").strip()
    if not raw_spec:
        return None
    spec = resolve_alias(raw_spec)
    rest = match.group("rest").lstrip()
    if not rest:
        return None
    return InlineOverride(spec=spec, cleaned_content=rest)


def parse_slash_command(content: str) -> SlashCommand | None:
    """Recognize `/brain ...` slash commands."""
    if _SLASH_HELP_RE.match(content or ""):
        return SlashCommand(
            kind="help",
            reply=(
                "Usage: /brain <name>\n"
                "Examples: /brain opus, /brain sonnet, /brain codex:gpt-5\n"
                "Sticky brain stays active until the conversation goes idle."
            ),
        )
    match = _SLASH_BRAIN_RE.match(content or "")
    if not match:
        return None
    raw_spec = match.group("spec").strip()
    spec = resolve_alias(raw_spec)
    return SlashCommand(
        kind="brain",
        spec=spec,
        reply=f"OK — sticky brain set to `{spec}`. Subsequent messages will use it until the chat goes idle.",
    )
