"""Config loader for the supervisor block (ops/gateway.yaml → supervisor:)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from watchdog.registry import _parse_yaml


_DEFAULT_THRESHOLDS: dict[str, int] = {
    "claude": 30,
    "codex": 90,
    "pi": 45,
    "openrouter": 30,
    "default": 60,
}


@dataclass(frozen=True)
class SupervisorConfig:
    enabled: bool = True
    tick_interval_seconds: int = 30
    notice_threshold_seconds: dict[str, int] = field(
        default_factory=lambda: dict(_DEFAULT_THRESHOLDS)
    )
    min_card_interval_seconds: int = 15
    max_cards_per_event: int = 12
    narrator_brain: str = "openrouter:deepseek-v4-flash"
    narrator_calls_per_tick_max: int = 5
    narrator_calls_per_event_max: int = 6
    stderr_tail_bytes: int = 4096
    phases_table: str = ""
    recovery_patterns: str = ""
    recovery_enabled: bool = True
    max_recovery_attempts: int = 2
    groups_enabled: bool = False
    groups_show_phase: bool = False
    channels_enabled: dict[str, bool] = field(
        default_factory=lambda: {
            "telegram": True,
            "slack": True,
            "discord": True,
            "voice": False,
            "cron": False,
            "jc-events": False,
            "email": False,
        }
    )

    def notice_threshold(self, brain: str) -> int:
        return self.notice_threshold_seconds.get(
            brain, self.notice_threshold_seconds.get("default", 60)
        )

    def channel_enabled(self, channel: str) -> bool:
        return self.channels_enabled.get(channel, False)


def load_config(instance_dir: Path) -> SupervisorConfig:
    path = instance_dir / "ops" / "gateway.yaml"
    if not path.exists():
        return SupervisorConfig()
    try:
        data = _parse_yaml(path.read_text(encoding="utf-8"))
    except Exception:
        return SupervisorConfig()
    raw = data.get("supervisor")
    if not isinstance(raw, dict):
        return SupervisorConfig()
    return _parse(raw)


def _parse(raw: dict[str, Any]) -> SupervisorConfig:
    thresholds = _parse_thresholds(raw.get("notice_threshold_seconds"))

    channels: dict[str, bool] = {
        "telegram": True,
        "slack": True,
        "discord": True,
        "voice": False,
        "cron": False,
        "jc-events": False,
        "email": False,
    }
    raw_channels = raw.get("channels")
    if isinstance(raw_channels, dict):
        for k, v in raw_channels.items():
            channels[str(k)] = _bool(v, channels.get(str(k), False))

    groups_raw = raw.get("groups") or {}
    recovery_raw = raw.get("recovery") or {}

    return SupervisorConfig(
        enabled=_bool(raw.get("enabled"), True),
        tick_interval_seconds=_int(raw.get("tick_interval_seconds"), 30),
        notice_threshold_seconds=thresholds,
        min_card_interval_seconds=_int(raw.get("min_card_interval_seconds"), 15),
        max_cards_per_event=_int(raw.get("max_cards_per_event"), 12),
        narrator_brain=str(raw.get("narrator_brain") or "openrouter:deepseek-v4-flash"),
        narrator_calls_per_tick_max=_int(raw.get("narrator_calls_per_tick_max"), 5),
        narrator_calls_per_event_max=_int(raw.get("narrator_calls_per_event_max"), 6),
        stderr_tail_bytes=_int(raw.get("stderr_tail_bytes"), 4096),
        phases_table=str(raw.get("phases_table") or ""),
        recovery_patterns=str(raw.get("recovery_patterns") or ""),
        recovery_enabled=_bool((recovery_raw or {}).get("enabled"), True),
        max_recovery_attempts=_int((recovery_raw or {}).get("max_recovery_attempts"), 2),
        groups_enabled=_bool((groups_raw or {}).get("enabled"), False),
        groups_show_phase=_bool((groups_raw or {}).get("show_phase"), False),
        channels_enabled=channels,
    )


def _parse_thresholds(raw_thresh: Any) -> dict[str, int]:
    """Merge user threshold block with hardcoded defaults.

    Priority: user per-brain > user `default` > hardcoded per-brain > hardcoded default.
    When user provides a `default` key, it overrides hardcoded per-brain values for
    any brain not explicitly set in the user block.
    """
    if not isinstance(raw_thresh, dict):
        return dict(_DEFAULT_THRESHOLDS)

    user: dict[str, int] = {}
    for k, v in raw_thresh.items():
        try:
            user[str(k)] = int(v)
        except (TypeError, ValueError):
            pass

    user_default = user.get("default")
    all_brains = set(_DEFAULT_THRESHOLDS) | set(user)
    result: dict[str, int] = {}
    for brain in all_brains:
        if brain in user:
            result[brain] = user[brain]
        elif user_default is not None:
            result[brain] = user_default
        else:
            result[brain] = _DEFAULT_THRESHOLDS.get(brain, _DEFAULT_THRESHOLDS["default"])
    return result


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
