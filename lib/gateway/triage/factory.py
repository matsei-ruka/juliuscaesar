"""Build a triage backend from `TriageConfig`."""

from __future__ import annotations

from pathlib import Path

from ..config import CodexAuthConfig, TriageConfig
from .base import TriageBackend
from .claude_channel import ClaudeChannelTriage
from .codex_api import CodexApiTriage
from .ollama import OllamaTriage
from .openrouter import OpenRouterTriage


class _NoneBackend(TriageBackend):
    name = "none"

    def classify(self, message: str):  # pragma: no cover - trivial
        from .base import TriageResult

        return TriageResult(class_="quick", brain="", confidence=0.0, reasoning="triage disabled")


def build_backend(
    cfg: TriageConfig,
    instance_dir: Path,
    *,
    codex_auth_cfg: CodexAuthConfig | None = None,
) -> TriageBackend:
    backend = (cfg.backend or "none").strip().lower()
    if backend in ("none", "always", ""):
        return _NoneBackend()
    if backend == "ollama":
        return OllamaTriage(cfg)
    if backend == "openrouter":
        return OpenRouterTriage(cfg, instance_dir)
    if backend == "claude-channel":
        return ClaudeChannelTriage(cfg)
    if backend in ("codex_api", "codex-api"):
        return CodexApiTriage(cfg, instance_dir, codex_auth_cfg=codex_auth_cfg)
    raise ValueError(f"unknown triage backend: {backend}")
