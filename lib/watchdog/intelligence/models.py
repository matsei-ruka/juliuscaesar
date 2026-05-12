"""Data records for intelligent watchdog decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from gateway.queue import Event


DecisionKind = Literal[
    "healthy",
    "brain_unhealthy",
    "auth_expired",
    "long_running",
    "transient_slow",
    "unknown",
]

Severity = Literal["info", "warning", "critical"]


@dataclass(frozen=True)
class EventSummary:
    event: Event
    meta: dict[str, Any]
    age_seconds: float
    brain: str
    model: str | None = None
    status: str = ""
    error: str = ""

    @property
    def brain_spec(self) -> str:
        return f"{self.brain}:{self.model}" if self.model else self.brain


@dataclass(frozen=True)
class LogEntry:
    raw: str
    ts: str = ""
    msg: str = ""
    kind: str = ""
    event_id: int | None = None
    brain: str = ""
    model: str = ""


@dataclass(frozen=True)
class Snapshot:
    running: list[EventSummary] = field(default_factory=list)
    failed: list[EventSummary] = field(default_factory=list)
    logs: list[LogEntry] = field(default_factory=list)


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    confidence: float
    severity: Severity
    user_visible: bool
    should_switch_brain: bool
    summary: str
    source: str = "heuristic"

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "confidence": self.confidence,
            "severity": self.severity,
            "user_visible": self.user_visible,
            "should_switch_brain": self.should_switch_brain,
            "summary": self.summary,
            "source": self.source,
        }

