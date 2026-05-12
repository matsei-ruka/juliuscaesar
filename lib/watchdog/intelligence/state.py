"""Persistent state for intelligent watchdog dedupe and brain cooldowns."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def state_path(instance_dir: Path) -> Path:
    return instance_dir / "state" / "watchdog" / "intelligence.json"


@dataclass
class IntelligenceState:
    notified_events: dict[str, dict[str, str]] = field(default_factory=dict)
    brain_health: dict[str, dict[str, str]] = field(default_factory=dict)
    latest_decisions: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, instance_dir: Path) -> "IntelligenceState":
        path = state_path(instance_dir)
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        return cls(
            notified_events=_dict_of_dict(raw.get("notified_events")),
            brain_health=_dict_of_dict(raw.get("brain_health")),
            latest_decisions=list(raw.get("latest_decisions") or [])[-50:],
        )

    def save(self, instance_dir: Path) -> None:
        path = state_path(instance_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "notified_events": self.notified_events,
            "brain_health": self.brain_health,
            "latest_decisions": self.latest_decisions[-50:],
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(path)

    def has_notice(self, event_id: int, key: str) -> bool:
        return bool(self.notified_events.get(str(event_id), {}).get(key))

    def mark_notice(self, event_id: int, key: str, *, at: str | None = None) -> None:
        row = self.notified_events.setdefault(str(event_id), {})
        row[key] = at or now_iso()

    def mark_brain_unavailable(self, brain: str, *, reason: str, until: str) -> None:
        if not brain:
            return
        self.brain_health[brain] = {
            "state": "unavailable",
            "reason": reason,
            "until": until,
        }

    def clear_brain(self, brain: str) -> None:
        self.brain_health.pop(brain, None)

    def is_brain_unavailable(self, brain: str, *, now: datetime | None = None) -> bool:
        mark = self.brain_health.get(brain)
        if not mark:
            return False
        until = parse_iso(mark.get("until"))
        if until is None:
            return mark.get("state") == "unavailable"
        now = now or datetime.now(timezone.utc)
        if until <= now:
            self.brain_health.pop(brain, None)
            return False
        return mark.get("state") == "unavailable"

    def record_decision(self, event_id: int | None, decision: dict[str, Any]) -> None:
        row = {"at": now_iso(), **decision}
        if event_id is not None:
            row["event_id"] = event_id
        self.latest_decisions.append(row)
        self.latest_decisions = self.latest_decisions[-50:]


def _dict_of_dict(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, body in value.items():
        if isinstance(body, dict):
            out[str(key)] = {str(k): str(v) for k, v in body.items()}
    return out

