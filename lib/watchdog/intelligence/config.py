"""Config loader for the intelligent watchdog block."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from watchdog.registry import _parse_yaml, registry_path


@dataclass(frozen=True)
class IntelligenceConfig:
    enabled: bool = True
    long_running_notice_seconds: int = 180
    brain_health_cooldown_seconds: int = 900
    log_window_seconds: int = 900
    log_window_lines: int = 200


def load_config(instance_dir: Path) -> IntelligenceConfig:
    path = registry_path(instance_dir)
    if not path.exists():
        return IntelligenceConfig()
    data = _parse_yaml(path.read_text(encoding="utf-8"))
    raw = data.get("watchdog")
    if not isinstance(raw, dict):
        return IntelligenceConfig()
    return IntelligenceConfig(
        enabled=_bool(raw.get("intelligent"), True),
        long_running_notice_seconds=_int(raw.get("long_running_notice_seconds"), 180),
        brain_health_cooldown_seconds=_int(
            raw.get("brain_health_cooldown_seconds", raw.get("brain_switch_cooldown_seconds")),
            900,
        ),
        log_window_seconds=_int(raw.get("log_window_seconds"), 900),
        log_window_lines=_int(raw.get("log_window_lines"), 200),
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
