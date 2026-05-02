"""Brain capability matrix.

Single source of truth for what each brain can handle. Used by the runtime
to decide whether to route an event to its selected brain or fall back to a
multimodal-capable brain (e.g. when the event has an image and the selected
brain is text-only).

Per docs/specs/codex-main-brain-hardening.md §"Images and multimodal input"
(capability matrix v1). Update this table when a brain gains/loses a
capability instead of sprinkling new branch checks elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BrainCapabilities:
    text: bool = True
    images: bool = False
    tools: bool = False
    file_edits: bool = False


_CAPABILITIES: dict[str, BrainCapabilities] = {
    "claude":    BrainCapabilities(text=True, images=True,  tools=True, file_edits=True),
    "codex":     BrainCapabilities(text=True, images=True,  tools=True, file_edits=True),
    "codex_api": BrainCapabilities(text=True, images=False, tools=False, file_edits=False),
    "gemini":    BrainCapabilities(text=True, images=True,  tools=True, file_edits=True),
    "opencode":  BrainCapabilities(text=True, images=False, tools=True, file_edits=True),
    "aider":     BrainCapabilities(text=True, images=False, tools=True, file_edits=True),
}

_UNKNOWN = BrainCapabilities(text=True, images=False, tools=False, file_edits=False)


def for_brain(brain: str) -> BrainCapabilities:
    return _CAPABILITIES.get(brain, _UNKNOWN)


def supports_images(brain: str) -> bool:
    return for_brain(brain).images
