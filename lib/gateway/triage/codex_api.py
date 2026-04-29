"""Triage backend that classifies via OpenAI's Responses API using the Codex
CLI's OAuth state for auth — no extra API key required.

Picks ``gpt-4o-mini`` by default (cheapest reasoning-capable). Operators can
override via ``triage.openrouter_model`` — we reuse that field as the model
name to avoid bloating the schema with another knob.
"""

from __future__ import annotations

from pathlib import Path

from codex_auth import CodexAuthClient, ResponsesClient, ResponsesError
from codex_auth.errors import CodexAuthError

from ..config import CodexAuthConfig, TriageConfig
from .base import TriageBackend, TriageResult, parse_triage_json, render_prompt


# ChatGPT-subscription Responses uses the Codex model catalog; the spec's
# nominal ``gpt-4o-mini`` choice is unreachable. ``gpt-5.4-mini`` is the
# cheapest catalog entry and works as a triage classifier.
DEFAULT_TRIAGE_MODEL = "gpt-5.4-mini"
SYSTEM_INSTRUCTIONS = "Output exactly one JSON object on one line."


class CodexApiTriage(TriageBackend):
    name = "codex_api"

    def __init__(
        self,
        cfg: TriageConfig,
        instance_dir: Path,
        *,
        codex_auth_cfg: CodexAuthConfig | None = None,
        client: ResponsesClient | None = None,
        model_override: str | None = None,
    ):
        self.cfg = cfg
        self.instance_dir = instance_dir
        self._model = model_override or _model_from_cfg(cfg)
        if client is not None:
            self._client = client
        else:
            ca = codex_auth_cfg or CodexAuthConfig()
            self._client = ResponsesClient(
                CodexAuthClient(
                    auth_file=ca.auth_file,
                    client_id_override=ca.client_id_override,
                    refresh_skew_seconds=ca.refresh_skew_seconds,
                ),
                default_model=self._model,
                timeout_seconds=max(5, int(cfg.openrouter_timeout_seconds or 10)),
            )

    def classify(self, message: str) -> TriageResult:
        prompt = render_prompt(message)
        try:
            result = self._client.complete(
                prompt,
                model=self._model,
                instructions=SYSTEM_INSTRUCTIONS,
                max_output_tokens=200,
            )
        except (ResponsesError, CodexAuthError) as exc:
            return _failure(f"codex_api unreachable: {exc}")
        text = result.text or ""
        parsed = parse_triage_json(text)
        if parsed is not None:
            return parsed
        return _failure("codex_api unparseable triage output", raw=text)


def _model_from_cfg(cfg: TriageConfig) -> str:
    # Operators can repurpose the openrouter_model field to override the
    # triage model when running through codex_api. If left at default, fall
    # back to the spec's launch model. Accept Codex catalog slugs (gpt-5.x*)
    # as well as legacy gpt-4* slugs in case the catalog rotates.
    candidate = (cfg.openrouter_model or "").strip()
    if candidate and "/" not in candidate and candidate.startswith(("gpt-", "o", "codex-")):
        return candidate
    return DEFAULT_TRIAGE_MODEL


def _failure(reason: str, *, raw: str | None = None) -> TriageResult:
    return TriageResult(
        class_="quick",
        brain="claude:sonnet-4-6",
        confidence=0.0,
        reasoning=reason,
        raw=(raw or "")[:400] if raw else None,
    )
