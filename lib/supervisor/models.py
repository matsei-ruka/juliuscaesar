"""Data records for supervisor snapshots and tick results."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gateway.queue import Event


@dataclass(frozen=True)
class PhaseResult:
    phase: str
    emoji: str
    label: dict[str, str]

    def label_for(self, lang: str) -> str:
        return self.label.get(lang) or self.label.get("en") or self.phase


@dataclass(frozen=True)
class AdapterInfo:
    event_id: int
    pid: int | None
    stderr_path: Path | None
    stderr_mtime: float | None
    stderr_tail: str
    pid_alive: bool
    stdout_path: Path | None = None
    stdout_tail: str = ""

    @property
    def activity_age_seconds(self) -> float | None:
        if self.stderr_mtime is None:
            return None
        return max(0.0, time.time() - self.stderr_mtime)


@dataclass(frozen=True)
class EventSnapshot:
    event: Event
    meta: dict[str, Any]
    age_seconds: float
    brain: str
    model: str | None
    adapter: AdapterInfo
    phase: PhaseResult
    worker_linked: bool
    language: str
    slot: int | None = None

    @property
    def brain_spec(self) -> str:
        return f"{self.brain}:{self.model}" if self.model else self.brain


@dataclass
class TickResult:
    enabled: bool
    snapshots: list[EventSnapshot] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    recoveries: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "qualifying": len(self.snapshots),
            "skipped": len(self.skipped),
            "recoveries": list(self.recoveries),
            "error": self.error,
            "events": [
                {
                    "event_id": s.event.id,
                    "brain": s.brain_spec,
                    "age_seconds": round(s.age_seconds, 1),
                    "phase": s.phase.phase,
                    "emoji": s.phase.emoji,
                    "worker_linked": s.worker_linked,
                    "pid_alive": s.adapter.pid_alive,
                    "language": s.language,
                }
                for s in self.snapshots
            ],
        }
