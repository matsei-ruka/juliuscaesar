"""Dispatch entry point for brain invocation.

Looks up the per-brain Python wrapper, applies any `brains.<name>` config
override, and runs it. Falls back to the legacy `gateway.brain.call_brain`
implementation when a brain has no Python wrapper yet.
"""

from __future__ import annotations

from pathlib import Path

from ..config import BrainOverrideConfig, GatewayConfig
from ..queue import Event
from .aider import AiderBrain
from .base import Brain, BrainResult
from .claude import ClaudeBrain
from .codex import CodexBrain
from .gemini import GeminiBrain
from .opencode import OpencodeBrain


_BRAIN_REGISTRY: dict[str, type[Brain]] = {
    "claude": ClaudeBrain,
    "codex": CodexBrain,
    "gemini": GeminiBrain,
    "opencode": OpencodeBrain,
    "aider": AiderBrain,
}


def supported_brains() -> tuple[str, ...]:
    return tuple(_BRAIN_REGISTRY.keys())


def invoke_brain(
    *,
    instance_dir: Path,
    event: Event,
    brain: str,
    model: str | None,
    resume_session: str | None,
    timeout_seconds: int,
    log_path: Path,
    config: GatewayConfig | None = None,
) -> BrainResult:
    cls = _BRAIN_REGISTRY.get(brain)
    if cls is None:
        raise ValueError(f"unsupported brain: {brain}")
    override = (config.brains.get(brain) if config else None) or BrainOverrideConfig()
    instance = cls(instance_dir, override=override)
    return instance.invoke(
        event=event,
        model=model,
        resume_session=resume_session,
        timeout_seconds=timeout_seconds,
        log_path=log_path,
    )
