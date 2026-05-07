"""Wire-protocol helpers for HTTP triage classifiers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


SYSTEM_INSTRUCTIONS = "Output exactly one JSON object on one line."


class Protocol(ABC):
    name: str

    @abstractmethod
    def url(self, base_url: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def headers(self, api_key: str) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    def encode(self, prompt: str, *, model: str, max_tokens: int | None) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def decode(self, payload: dict[str, Any]) -> str:
        raise NotImplementedError
