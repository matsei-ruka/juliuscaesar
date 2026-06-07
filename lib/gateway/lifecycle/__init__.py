"""Context-aware session lifecycle.

Implements docs/specs/context-aware-session-lifecycle.md:

  - §8  context telemetry (`telemetry.py`);
  - §9  context profiles + session ceiling (`profiles.py`);
  - §11 pre-dispatch routing/lifecycle pressure gate (`routing.py`);
  - §18  conversation-scoped `/compact` orchestration (`compaction.py`);
  - §18.1 operator notification on compaction (`notify.py`).

Provider sessions are bounded working memory. Continuity is owned by JC
through transcripts, checkpoints, and L1/L2 memory; this package measures
context pressure and rotates provider sessions before they become unusable.
"""

from __future__ import annotations

from .profiles import ContextProfile, ProfileRegistry, session_ceiling
from .routing import GuardDecision, evaluate_pressure, lifecycle_pressure, routing_pressure
from .telemetry import ContextUsage, SessionTelemetry, get_telemetry, record_usage

__all__ = [
    "ContextProfile",
    "ContextUsage",
    "GuardDecision",
    "ProfileRegistry",
    "SessionTelemetry",
    "evaluate_pressure",
    "get_telemetry",
    "lifecycle_pressure",
    "record_usage",
    "routing_pressure",
    "session_ceiling",
]
