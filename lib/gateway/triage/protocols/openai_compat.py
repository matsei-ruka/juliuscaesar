"""OpenAI-compatible chat-completions protocol."""

from __future__ import annotations

from typing import Any

from .base import Protocol, SYSTEM_INSTRUCTIONS


class OpenAICompatProtocol(Protocol):
    name = "openai_compat"

    def url(self, base_url: str) -> str:
        return base_url.rstrip("/") + "/chat/completions"

    def headers(self, api_key: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

    def encode(self, prompt: str, *, model: str, max_tokens: int | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return body

    def decode(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices:
            raise ValueError("empty choices")
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")
