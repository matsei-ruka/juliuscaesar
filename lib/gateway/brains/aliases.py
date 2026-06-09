"""Short-name aliases for brain specifications.

Used by the brain-override prefix parser (`[opus] ...`) and the `/brain`
slash command to resolve user-friendly names to canonical `<brain>:<model>`
specs that the router and dispatch understand.
"""

from __future__ import annotations


SHORT_NAME_ALIASES: dict[str, str] = {
    "opus": "claude:opus-4-8",
    "opus48": "claude:opus-4-8",
    "opus-4-8": "claude:opus-4-8",
    "opus47": "claude:opus-4-7-1m",
    "opus-4-7": "claude:opus-4-7",
    "fable": "claude:fable-5",
    "fable5": "claude:fable-5",
    "claude-fable": "claude:fable-5",
    "sonnet": "claude:sonnet-4-6",
    "sonnet46": "claude:sonnet-4-6",
    "haiku": "claude:haiku-4-5",
    "haiku45": "claude:haiku-4-5",
    "claude": "claude",
    "codex": "codex",
    "gpt5": "codex:gpt-5.4",
    "gpt-5": "codex:gpt-5.4",
    "gpt54": "codex:gpt-5.4",
    "mini": "codex:gpt-5.4-mini",
    "codex-mini": "codex:gpt-5.4-mini",
    "codex-coding": "codex:gpt-5.3-codex",
    "gpt4o": "codex:gpt-4o",
    "gemini": "gemini",
    "gemini25": "gemini:gemini-2.5-pro",
    "gemini20": "gemini:gemini-2.0-flash",
    "opencode": "opencode",
    "aider": "aider",
    "grok": "grok",
    "grok-build": "grok:grok-build",
    "grok-fast": "grok:fast",
    "grok-composer": "grok:fast",
    "pi": "pi",
    "pi-sonnet": "pi:sonnet",
    "pi-opus": "pi:opus",
    "pi-haiku": "pi:haiku",
    "pi-gpt5": "pi:gpt-5.4",
    "pi-mini": "pi:gpt-5.4-mini",
    "pi-google": "pi:gemini-2.5-pro",
    "pi-gemini": "pi:gemini-2.5-pro",
    "pi-gemini25": "pi:gemini-2.5-pro",
    "pi-gemini20": "pi:gemini-2.0-flash",
}


def resolve_alias(name: str) -> str:
    """Return the canonical `<brain>[:<model>]` spec for `name`.

    Unknown names are returned unchanged — the router handles them as raw
    brain specs and dispatch will fail loudly if the brain is unsupported.
    """

    if not name:
        return name
    key = name.strip().lower()
    return SHORT_NAME_ALIASES.get(key, name.strip())
