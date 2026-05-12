"""Configuration loader for re-engagement."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore


@dataclass(frozen=True)
class QuietHours:
    start: str = "23:00"
    end: str = "07:00"


@dataclass(frozen=True)
class TrackedChat:
    chat_id: int
    name: str = ""
    templates: dict[str, str] = field(default_factory=dict)
    allowed_slots: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReengageConfig:
    enabled: bool = False
    scan_interval_hours: int = 6
    silence_threshold_hours: int = 48
    max_touches: int = 4
    touch_schedule: tuple[int, ...] = (48, 72, 96, 120)
    allowed_slots: tuple[str, ...] = ("07:00", "19:00")
    quiet_hours: QuietHours = field(default_factory=QuietHours)
    tracked_chats: tuple[TrackedChat, ...] = ()
    timezone: str = "UTC"

    def chat(self, chat_id: int | str) -> TrackedChat | None:
        wanted = str(chat_id)
        for chat in self.tracked_chats:
            if str(chat.chat_id) == wanted:
                return chat
        return None


def load_config(instance_dir: Path) -> ReengageConfig:
    cfg_path = instance_dir / "ops" / "reengage.yaml"
    if not cfg_path.exists():
        return ReengageConfig(timezone=_instance_timezone(instance_dir))
    if yaml is None:
        raise ImportError("PyYAML required to load ops/reengage.yaml")
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return ReengageConfig(timezone=_instance_timezone(instance_dir))
    if not isinstance(data, dict):
        return ReengageConfig(timezone=_instance_timezone(instance_dir))
    return _build(data, timezone=_instance_timezone(instance_dir))


def _build(data: dict, *, timezone: str) -> ReengageConfig:
    qh = data.get("quiet_hours") or {}
    tracked: list[TrackedChat] = []
    for raw in data.get("tracked_chats") or []:
        if not isinstance(raw, dict) or raw.get("chat_id") in (None, ""):
            continue
        templates = raw.get("templates") or {}
        tracked.append(
            TrackedChat(
                chat_id=int(raw["chat_id"]),
                name=str(raw.get("name") or ""),
                templates={str(k): str(v) for k, v in templates.items()},
                allowed_slots=tuple(str(v) for v in raw.get("allowed_slots") or ()),
            )
        )
    schedule = tuple(int(v) for v in data.get("touch_schedule") or (48, 72, 96, 120))
    slots = tuple(str(v) for v in data.get("allowed_slots") or ("07:00", "19:00"))
    return ReengageConfig(
        enabled=bool(data.get("enabled", False)),
        scan_interval_hours=int(data.get("scan_interval_hours", 6) or 6),
        silence_threshold_hours=int(data.get("silence_threshold_hours", 48) or 48),
        max_touches=int(data.get("max_touches", 4) or 4),
        touch_schedule=schedule,
        allowed_slots=slots,
        quiet_hours=QuietHours(start=str(qh.get("start") or "23:00"), end=str(qh.get("end") or "07:00")),
        tracked_chats=tuple(tracked),
        timezone=timezone,
    )


def zoneinfo(cfg: ReengageConfig) -> ZoneInfo:
    try:
        return ZoneInfo(cfg.timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _instance_timezone(instance_dir: Path) -> str:
    env_tz = _env_value(instance_dir / ".env", "TZ")
    if env_tz:
        return env_tz
    try:
        from gateway.config import load_config_cached  # type: ignore

        return load_config_cached(instance_dir).timezone or "UTC"
    except Exception:
        return os.environ.get("TZ") or "UTC"


def _env_value(path: Path, key: str) -> str:
    if not path.exists():
        return ""
    prefix = f"{key}="
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if (value.startswith("'") and value.endswith("'")) or (
            value.startswith('"') and value.endswith('"')
        ):
            value = value[1:-1]
        return value
    return ""
