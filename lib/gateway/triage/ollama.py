"""Ollama triage backend.

Posts the rendered prompt to `${ollama_host}/api/generate`. Returns a
permissive low-confidence quick-class result on any HTTP / parse failure so
the router falls back gracefully.
"""

from __future__ import annotations

import json
import urllib.request
from urllib.error import URLError

from ..config import TriageConfig
from .base import TriageBackend, TriageResult, parse_triage_json, render_prompt


class OllamaTriage(TriageBackend):
    name = "ollama"

    def __init__(self, cfg: TriageConfig):
        self.cfg = cfg

    def classify(self, message: str) -> TriageResult:
        prompt = render_prompt(message)
        body = {
            "model": self.cfg.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0},
        }
        url = self.cfg.ollama_host.rstrip("/") + "/api/generate"
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.cfg.ollama_timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (URLError, TimeoutError, ConnectionError) as exc:
            return _failure(f"ollama unreachable: {exc}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return _failure(f"ollama bad response: {exc}")
        text = str(payload.get("response") or "")
        result = parse_triage_json(text)
        if result is not None:
            return result
        return _failure("ollama unparseable triage output", raw=text)


def _failure(reason: str, *, raw: str | None = None) -> TriageResult:
    return TriageResult(
        class_="quick",
        brain="claude:sonnet-4-6",
        confidence=0.0,
        reasoning=reason,
        raw=(raw or "")[:400] if raw else None,
    )
