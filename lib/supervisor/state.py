"""Persistent supervisor state (state/supervisor/state.json)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class EventState:
    first_card_at: float = 0.0
    last_card_at: float = 0.0
    last_phase: str = ""
    channel_message_id: str | None = None
    narration_count: int = 0
    last_narration: str = ""
    recovery_attempts: int = 0
    escalated: bool = False
    language: str = "en"
    card_count: int = 0


_EVENT_FIELDS = set(EventState.__dataclass_fields__)


@dataclass
class SupervisorState:
    events: dict[str, EventState] = field(default_factory=dict)
    last_tick_at: float = 0.0

    def event(self, event_id: int) -> EventState:
        key = str(event_id)
        if key not in self.events:
            self.events[key] = EventState()
        return self.events[key]

    def prune(self, active_ids: set[int]) -> None:
        active_keys = {str(i) for i in active_ids}
        stale = [k for k in list(self.events) if k not in active_keys]
        for k in stale:
            del self.events[k]

    @classmethod
    def load(cls, instance_dir: Path) -> "SupervisorState":
        path = _state_path(instance_dir)
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        events: dict[str, EventState] = {}
        for k, v in (data.get("events") or {}).items():
            if isinstance(v, dict):
                kwargs = {f: v[f] for f in _EVENT_FIELDS if f in v}
                events[k] = EventState(**kwargs)
        return cls(
            events=events,
            last_tick_at=float(data.get("last_tick_at") or 0),
        )

    def save(self, instance_dir: Path) -> None:
        path = _state_path(instance_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "last_tick_at": self.last_tick_at,
            "events": {k: asdict(v) for k, v in self.events.items()},
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def _state_path(instance_dir: Path) -> Path:
    return instance_dir / "state" / "supervisor" / "state.json"
