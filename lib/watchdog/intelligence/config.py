"""Config loader for the intelligent watchdog block."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from watchdog.registry import _parse_yaml, registry_path


DEFAULT_BRAIN_FALLBACKS = {
    "claude": ("codex", "gemini", "opencode"),
    "codex": ("claude", "gemini"),
    "codex_api": ("claude", "codex"),
    "gemini": ("claude", "codex"),
    "opencode": ("claude", "codex"),
    "aider": ("claude", "codex"),
}


@dataclass(frozen=True)
class IntelligenceConfig:
    enabled: bool = True
    brain_switch_enabled: bool = True
    long_running_notice_seconds: int = 180
    long_running_repeat_seconds: int = 0
    brain_switch_cooldown_seconds: int = 900
    log_window_seconds: int = 900
    log_window_lines: int = 200
    failed_event_max_age_seconds: int = 3600
    failed_event_limit: int = 50
    use_triage_model: bool = True
    brain_fallbacks: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_BRAIN_FALLBACKS)
    )


def load_config(instance_dir: Path) -> IntelligenceConfig:
    path = registry_path(instance_dir)
    if not path.exists():
        return IntelligenceConfig()
    data = _parse_yaml(path.read_text(encoding="utf-8"))
    raw = data.get("watchdog")
    if not isinstance(raw, dict):
        return IntelligenceConfig()
    fallbacks = dict(DEFAULT_BRAIN_FALLBACKS)
    raw_fallbacks = raw.get("brain_fallbacks")
    if isinstance(raw_fallbacks, dict):
        for brain, values in raw_fallbacks.items():
            fallbacks[str(brain)] = tuple(str(v) for v in _as_list(values) if str(v).strip())
    return IntelligenceConfig(
        enabled=_bool(raw.get("intelligent"), True),
        brain_switch_enabled=_bool(raw.get("brain_switch_enabled"), True),
        long_running_notice_seconds=_int(raw.get("long_running_notice_seconds"), 180),
        long_running_repeat_seconds=_int(raw.get("long_running_repeat_seconds"), 0),
        brain_switch_cooldown_seconds=_int(raw.get("brain_switch_cooldown_seconds"), 900),
        log_window_seconds=_int(raw.get("log_window_seconds"), 900),
        log_window_lines=_int(raw.get("log_window_lines"), 200),
        failed_event_max_age_seconds=_int(raw.get("failed_event_max_age_seconds"), 3600),
        failed_event_limit=_int(raw.get("failed_event_limit"), 50),
        use_triage_model=_bool(raw.get("use_triage_model"), True),
        brain_fallbacks=fallbacks,
    )


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]
