"""OpenRouter triage backend shim."""

from __future__ import annotations

from pathlib import Path

from ..config import TriageConfig
from .api_classifier import ApiClassifierTriage


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterTriage(ApiClassifierTriage):
    name = "openrouter"

    def __init__(self, cfg: TriageConfig, instance_dir: Path):
        super().__init__(
            cfg,
            instance_dir,
            protocol_name="openai_compat",
            base_url=OPENROUTER_BASE_URL,
            api_key_env=cfg.openrouter_api_key_env,
            model=cfg.openrouter_model,
            timeout_seconds=cfg.openrouter_timeout_seconds,
            extra_headers={
                "HTTP-Referer": "https://github.com/matsei-ruka/juliuscaesar",
                "X-Title": "JuliusCaesar Gateway",
            },
            name=self.name,
        )
