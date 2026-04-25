"""OpenRouter triage backend.

Uses OpenRouter's OpenAI-compatible chat completions endpoint. The API key is
read from the configured env var (default `OPENROUTER_API_KEY`).
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from urllib.error import URLError

from ..config import TriageConfig, env_value
from .base import TriageBackend, TriageResult, parse_triage_json, render_prompt


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterTriage(TriageBackend):
    name = "openrouter"

    def __init__(self, cfg: TriageConfig, instance_dir: Path):
        self.cfg = cfg
        self.instance_dir = instance_dir

    def _api_key(self) -> str:
        return env_value(self.instance_dir, self.cfg.openrouter_api_key_env)

    def classify(self, message: str) -> TriageResult:
        api_key = self._api_key()
        if not api_key:
            return _failure(f"missing {self.cfg.openrouter_api_key_env}")
        prompt = render_prompt(message)
        body = {
            "model": self.cfg.openrouter_model,
            "messages": [
                {"role": "system", "content": "Output exactly one JSON object on one line."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
        }
        try:
            req = urllib.request.Request(
                OPENROUTER_URL,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "HTTP-Referer": "https://github.com/matsei-ruka/juliuscaesar",
                    "X-Title": "JuliusCaesar Gateway",
                },
            )
            with urllib.request.urlopen(req, timeout=self.cfg.openrouter_timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (URLError, TimeoutError, ConnectionError) as exc:
            return _failure(f"openrouter unreachable: {exc}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return _failure(f"openrouter bad response: {exc}")
        choices = payload.get("choices") or []
        if not choices:
            return _failure("openrouter empty choices")
        message_data = choices[0].get("message") or {}
        text = str(message_data.get("content") or "")
        result = parse_triage_json(text)
        return result or _failure("openrouter unparseable triage output")


def _failure(reason: str) -> TriageResult:
    return TriageResult(
        class_="quick",
        brain="claude:sonnet-4-6",
        confidence=0.0,
        reasoning=reason,
    )
