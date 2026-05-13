"""Top-level intelligent watchdog tick."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from gateway.config import ConfigError, load_config as load_gateway_config

from . import actions
from .config import load_config as load_intelligence_config
from .evaluator import Evaluator
from .models import Decision, EventSummary, Snapshot
from .snapshot import build_snapshot
from .state import IntelligenceState


LogFn = Callable[[str], None]


@dataclass
class TickResult:
    enabled: bool
    snapshot: Snapshot | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "actions": self.actions,
            "decisions": self.decisions,
            "error": self.error,
            "running": len(self.snapshot.running) if self.snapshot else 0,
        }


def run_tick(
    instance_dir: Path,
    *,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> TickResult:
    log = log or (lambda _msg: None)
    intelligence_cfg = load_intelligence_config(instance_dir)
    if not intelligence_cfg.enabled:
        return TickResult(enabled=False)
    try:
        gateway_cfg = load_gateway_config(instance_dir)
    except ConfigError as exc:
        return TickResult(enabled=True, error=str(exc))
    state = IntelligenceState.load(instance_dir)
    snapshot = build_snapshot(instance_dir, intelligence_cfg)
    evaluator = Evaluator(
        instance_dir,
        gateway_config=gateway_cfg,
        intelligence_config=intelligence_cfg,
    )
    result = TickResult(enabled=True, snapshot=snapshot)
    for summary in snapshot.running:
        decision = evaluator.evaluate_event(snapshot, summary)
        _record_decision(result, state, summary, decision)
        if decision.kind in ("auth_expired", "brain_unhealthy"):
            _handle_unhealthy(
                instance_dir,
                intelligence_cfg=intelligence_cfg,
                gateway_cfg=gateway_cfg,
                state=state,
                summary=summary,
                decision=decision,
                result=result,
                dry_run=dry_run,
                log=log,
            )
    if not dry_run:
        state.save(instance_dir)
    return result


def _handle_unhealthy(
    instance_dir: Path,
    *,
    intelligence_cfg,
    gateway_cfg,
    state: IntelligenceState,
    summary: EventSummary,
    decision: Decision,
    result: TickResult,
    dry_run: bool,
    log: LogFn,
) -> None:
    if summary.brain != "unknown":
        actions.mark_brain_unavailable(
            state,
            summary.brain,
            reason=decision.kind,
            cooldown_seconds=intelligence_cfg.brain_health_cooldown_seconds,
        )
        result.actions.append(
            {"event_id": summary.event.id, "action": "brain_cooldown", "brain": summary.brain}
        )
    notice_key = "brain_issue_notice_at"
    if decision.user_visible and not _has_notice(state, summary, notice_key):
        if not dry_run:
            delivered = actions.notify_brain_issue(
                instance_dir,
                gateway_cfg,
                summary,
                decision=decision,
                log=log,
            )
            if delivered:
                actions.mark_event_notice(instance_dir, summary, notice_key)
                state.mark_notice(summary.event.id, notice_key)
        result.actions.append(
            {
                "event_id": summary.event.id,
                "action": "brain_issue_notice",
            }
        )


def _has_notice(state: IntelligenceState, summary: EventSummary, key: str) -> bool:
    if state.has_notice(summary.event.id, key):
        return True
    if summary.meta.get(key):
        return True
    notices = summary.meta.get("watchdog_notices")
    return isinstance(notices, dict) and bool(notices.get(key))


def _record_decision(
    result: TickResult,
    state: IntelligenceState,
    summary: EventSummary,
    decision: Decision,
) -> None:
    payload = decision.to_json()
    payload.update({"event_id": summary.event.id, "brain": summary.brain_spec, "status": summary.status})
    result.decisions.append(payload)
    state.record_decision(summary.event.id, payload)
