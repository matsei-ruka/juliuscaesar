"""Gateway brain adapters.

Sprint 1 ships a single dispatch entry point `invoke_brain` that wraps the
existing shell adapters in `lib/heartbeat/adapters/<tool>.sh`. Sprint 3
replaces the shell-only model with per-brain Python wrappers under this
package while preserving the same call signature.
"""

from .aider import AiderBrain
from .aliases import resolve_alias
from .base import AdapterFailure, Brain, BrainResult
from .claude import ClaudeBrain
from .codex import CodexBrain
from .dispatch import invoke_brain, supported_brains
from .gemini import GeminiBrain
from .opencode import OpencodeBrain


__all__ = [
    "AdapterFailure",
    "AiderBrain",
    "Brain",
    "BrainResult",
    "ClaudeBrain",
    "CodexBrain",
    "GeminiBrain",
    "OpencodeBrain",
    "invoke_brain",
    "resolve_alias",
    "supported_brains",
]
