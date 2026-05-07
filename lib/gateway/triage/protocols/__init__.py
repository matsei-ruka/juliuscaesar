"""Protocol registry for HTTP triage classifiers."""

from __future__ import annotations

from .anthropic import AnthropicProtocol
from .base import Protocol
from .openai_compat import OpenAICompatProtocol


PROTOCOLS: dict[str, Protocol] = {
    OpenAICompatProtocol.name: OpenAICompatProtocol(),
    AnthropicProtocol.name: AnthropicProtocol(),
}


def get_protocol(name: str) -> Protocol:
    try:
        return PROTOCOLS[name]
    except KeyError as exc:
        supported = ", ".join(sorted(PROTOCOLS))
        raise ValueError(f"unknown triage protocol {name!r} (supported: {supported})") from exc
