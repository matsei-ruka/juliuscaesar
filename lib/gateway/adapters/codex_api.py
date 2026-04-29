"""Main-chat adapter that calls OpenAI's Responses API directly using the
bearer token served by :class:`codex_auth.client.CodexAuthClient`.

The adapter is fronted by :class:`gateway.brains.codex_api.CodexApiBrain`
which integrates with the gateway's brain dispatch. The split exists so the
Brain class can stay focused on dispatch plumbing (env, logging, session id)
while the actual request-shaping logic lives here, mirroring the spec's
``lib/gateway/adapters/`` layout.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codex_auth import CodexAuthClient, ResponsesClient, ResponsesError
from codex_auth.errors import CodexAuthError

from ..config import CodexAuthConfig


# Cheapest Codex catalog model that the ChatGPT-subscription auth accepts.
# The public spec called for ``gpt-4o-mini`` but that catalog isn't
# reachable on the subscription Responses endpoint — see
# ``codex_auth.responses`` module docstring.
DEFAULT_MAIN_CHAT_MODEL = "gpt-5.4-mini"
DEFAULT_TIMEOUT_SECONDS = 60


@dataclass
class AdapterCallResult:
    text: str
    model: str
    usage: dict | None


class CodexApiAdapter:
    """High-level facade — feed ``run(prompt)`` and get back a text reply."""

    def __init__(
        self,
        *,
        codex_auth_cfg: CodexAuthConfig | None = None,
        default_model: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        client: ResponsesClient | None = None,
    ):
        self._model = default_model or DEFAULT_MAIN_CHAT_MODEL
        if client is not None:
            self._client = client
            return
        ca = codex_auth_cfg or CodexAuthConfig()
        self._client = ResponsesClient(
            CodexAuthClient(
                auth_file=ca.auth_file,
                client_id_override=ca.client_id_override,
                refresh_skew_seconds=ca.refresh_skew_seconds,
            ),
            default_model=self._model,
            timeout_seconds=timeout_seconds,
        )

    def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str | None = None,
        max_output_tokens: int | None = None,
    ) -> AdapterCallResult:
        try:
            result = self._client.complete(
                prompt,
                model=model or self._model,
                instructions=instructions,
                max_output_tokens=max_output_tokens,
            )
        except (ResponsesError, CodexAuthError):
            raise
        return AdapterCallResult(text=result.text, model=result.model, usage=result.usage)
