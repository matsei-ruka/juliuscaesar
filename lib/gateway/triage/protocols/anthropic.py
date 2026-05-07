"""Anthropic Messages protocol."""

from __future__ import annotations

from typing import Any

from .base import Protocol, SYSTEM_INSTRUCTIONS


class AnthropicProtocol(Protocol):
    name = "anthropic"

    def url(self, base_url: str) -> str:
        return base_url.rstrip("/") + "/messages"

    def headers(self, api_key: str) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }

    def encode(self, prompt: str, *, model: str, max_tokens: int | None) -> dict[str, Any]:
        if max_tokens is None:
            raise ValueError("anthropic protocol requires max_tokens")
        return {
            "model": model,
            "max_tokens": max_tokens,
            "system": SYSTEM_INSTRUCTIONS,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
        }

    def decode(self, payload: dict[str, Any]) -> str:
        parts = payload.get("content") or []
        if not isinstance(parts, list):
            raise ValueError("missing content")
        return "".join(
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        )
