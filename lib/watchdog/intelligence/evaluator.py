"""Deterministic evaluator for watchdog health decisions."""

from __future__ import annotations

import re
from pathlib import Path

from gateway.config import GatewayConfig

from .config import IntelligenceConfig
from .models import Decision, EventSummary, Snapshot


AUTH_RE = re.compile(
    r"(authentication|auth failed|please run.*login|/login|session (?:has )?expired|401|unauthorized|invalid api key|refresh token)",
    re.IGNORECASE,
)
TIMEOUT_RE = re.compile(r"(adapter timeout|timeout|timed out|deadline exceeded)", re.IGNORECASE)
FAIL_RE = re.compile(r"(dispatch failed|recovery fail|no models loaded|crash|traceback|non-zero|rc=)", re.IGNORECASE)


class Evaluator:
    def __init__(
        self,
        instance_dir: Path,
        *,
        gateway_config: GatewayConfig,
        intelligence_config: IntelligenceConfig,
    ):
        self.instance_dir = instance_dir
        self.gateway_config = gateway_config
        self.intelligence_config = intelligence_config

    def evaluate_event(self, snapshot: Snapshot, summary: EventSummary) -> Decision:
        text = "\n".join(
            [
                summary.error,
                *[
                    entry.msg or entry.raw
                    for entry in snapshot.logs
                    if entry.event_id in (None, summary.event.id)
                ],
            ]
        )
        if AUTH_RE.search(text):
            return Decision(
                kind="auth_expired",
                confidence=0.96,
                severity="critical",
                user_visible=True,
                should_switch_brain=True,
                summary="brain authentication appears expired",
                source="heuristic",
            )
        if FAIL_RE.search(text) and summary.status in ("failed", "queued"):
            return Decision(
                kind="brain_unhealthy",
                confidence=0.86,
                severity="warning",
                user_visible=True,
                should_switch_brain=True,
                summary="brain failed before completing the request",
                source="heuristic",
            )
        if TIMEOUT_RE.search(text) and summary.status in ("failed", "queued"):
            return Decision(
                kind="brain_unhealthy",
                confidence=0.82,
                severity="warning",
                user_visible=True,
                should_switch_brain=True,
                summary="brain adapter timed out",
                source="heuristic",
            )
        if summary.status == "running":
            return Decision(
                kind="long_running",
                confidence=0.78,
                severity="info",
                user_visible=False,
                should_switch_brain=False,
                summary="request is still running past the notice threshold",
                source="heuristic",
            )
        return Decision(
            kind="unknown",
            confidence=0.35,
            severity="info",
            user_visible=False,
            should_switch_brain=False,
            summary="insufficient watchdog evidence",
            source="heuristic",
        )
