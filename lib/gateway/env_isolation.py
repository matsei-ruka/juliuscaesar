"""Parent-env sanitizer for ``jc gateway start``.

Strips dangerous keys (sibling instance tokens, model keys, framework
overrides) before forking the daemon, then layers the instance ``.env``
on top. See ``docs/specs/gateway-env-isolation.md``.
"""

from __future__ import annotations

from typing import Callable, Mapping


DANGEROUS_PREFIXES: tuple[str, ...] = ("CODEX_", "CLAUDE_")
DANGEROUS_KEYS: frozenset[str] = frozenset({
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DASHSCOPE_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "COMPANY_API_KEY",
})

WHITELIST_KEYS: frozenset[str] = frozenset({
    "HOME",
    "USER",
    "LOGNAME",
    "PATH",
    "SHELL",
    "LANG",
    "TZ",
    "PWD",
    "TMPDIR",
    "TERM",
})
WHITELIST_PREFIXES: tuple[str, ...] = ("LC_",)


def is_dangerous(key: str) -> bool:
    if key in DANGEROUS_KEYS:
        return True
    return any(key.startswith(p) for p in DANGEROUS_PREFIXES)


def is_whitelisted(key: str) -> bool:
    if key in WHITELIST_KEYS:
        return True
    return any(key.startswith(p) for p in WHITELIST_PREFIXES)


def sanitize(
    parent_env: Mapping[str, str],
    dotenv: Mapping[str, str],
    *,
    key_allowed: Callable[[str], bool] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Return ``(clean_env, stripped_keys)``.

    ``clean_env`` = whitelisted parent keys + ``dotenv`` entries passing
    ``key_allowed``. ``stripped_keys`` = parent keys that matched
    ``is_dangerous`` and are not being re-supplied by ``dotenv`` (sorted,
    for stable logging).

    ``key_allowed`` (audit G-P2 / feature 8): without it, ``.env`` was
    layered wholesale and could inject PATH/LD_PRELOAD/JC_INSTANCE_DIR over
    the whitelisted parent env. ``bin/jc-gateway`` passes
    ``config.is_instance_env_key_allowed`` (the reserved-key predicate the
    rest of the framework already uses for .env values).
    """
    clean: dict[str, str] = {
        key: value for key, value in parent_env.items() if is_whitelisted(key)
    }
    stripped = sorted(
        key
        for key in parent_env
        if is_dangerous(key) and key not in dotenv
    )
    for key, value in dotenv.items():
        if key_allowed is not None and not key_allowed(key):
            continue
        clean[key] = value
    return clean, stripped
