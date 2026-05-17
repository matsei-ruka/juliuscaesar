"""Persistent supervisor state (state/supervisor/state.json)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# Cool-down window for stale (non-active) EventState entries that carry a
# non-zero recovery counter. Without it Bug #1 fires: an event that gets
# recovered leaves the snapshot set, prune drops its EventState, and the next
# claim creates a fresh EventState with recovery_attempts=0 — escalation never
# triggers for flapping events. 600s ≈ 20 ticks at the 30s default cadence.
RECOVERY_STATE_TTL_SECONDS = 600.0


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
    # Wall-clock timestamp (seconds since epoch) when the entry should become
    # eligible for prune. Set whenever recovery_attempts is bumped or the row
    # transitions out of the active set with non-zero counter; 0 means
    # "no pin — prune as usual".
    pinned_until: float = 0.0


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

    def prune(self, active_ids: set[int], *, now: float | None = None) -> None:
        """Drop entries not in ``active_ids``.

        Keep entries with non-zero ``recovery_attempts`` (or ``escalated``)
        until ``pinned_until`` elapses. This protects the recovery counter
        across one or more recover→requeue→reclaim cycles so escalation
        actually fires for flapping events (Bug #1).
        """
        import time as _time

        now = now if now is not None else _time.time()
        active_keys = {str(i) for i in active_ids}
        for k in list(self.events):
            if k in active_keys:
                continue
            ev = self.events[k]
            if ev.recovery_attempts > 0 or ev.escalated:
                if ev.pinned_until == 0.0:
                    ev.pinned_until = now + RECOVERY_STATE_TTL_SECONDS
                if now < ev.pinned_until:
                    continue
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
