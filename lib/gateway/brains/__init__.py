"""Gateway brain adapters.

Sprint 1 ships a single dispatch entry point `invoke_brain` that wraps the
existing shell adapters in `lib/heartbeat/adapters/<tool>.sh`. Sprint 3
replaces the shell-only model with per-brain Python wrappers under this
package while preserving the same call signature.
"""

from .aider import AiderBrain
from .aliases import resolve_alias
from .base import AdapterFailure, AdapterTimeout, Brain, BrainResult
from .claude import ClaudeBrain
from .codex import CodexBrain
from .dispatch import invoke_brain, supported_brains
from .gemini import GeminiBrain
from .grok import GrokBrain
from .opencode import OpencodeBrain
from .pi import PiBrain


__all__ = [
    "AdapterFailure",
    "AdapterTimeout",
    "AiderBrain",
    "Brain",
    "BrainResult",
    "ClaudeBrain",
    "CodexBrain",
    "GeminiBrain",
    "GrokBrain",
    "OpencodeBrain",
    "PiBrain",
    "invoke_brain",
    "resolve_alias",
    "supported_brains",
]
