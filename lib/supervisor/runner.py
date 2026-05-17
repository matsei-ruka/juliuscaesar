"""Supervisor tick orchestrator — Phase 1: snapshot + classify + log."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import load_config as load_supervisor_config
from .models import TickResult
from .snapshot import build_snapshots
from .state import SupervisorState


LogFn = Callable[[str], None]


def run_tick(
    instance_dir: Path,
    *,
    dry_run: bool = False,
    log: LogFn | None = None,
) -> TickResult:
    log = log or (lambda _: None)
    cfg = load_supervisor_config(instance_dir)
    if not cfg.enabled:
        return TickResult(enabled=False)

    now = datetime.now(timezone.utc)
    state = SupervisorState.load(instance_dir)

    # Throttle: noops if last tick was within interval
    if state.last_tick_at and (
        now.timestamp() - state.last_tick_at
    ) < cfg.tick_interval_seconds:
        return TickResult(enabled=True)

    _write_log(
        instance_dir,
        {"kind": "supervisor_tick_begin", "ts": now.isoformat()},
    )

    try:
        snapshots = build_snapshots(instance_dir, cfg, now=now)
    except Exception as exc:
        _write_log(
            instance_dir,
            {"kind": "supervisor_tick_error", "ts": now.isoformat(), "error": str(exc)},
        )
        return TickResult(enabled=True, error=str(exc))

    result = TickResult(enabled=True, snapshots=snapshots)

    for snap in snapshots:
        log(
            f"supervisor event={snap.event.id} brain={snap.brain_spec} "
            f"age={snap.age_seconds:.0f}s phase={snap.phase.phase} "
            f"emoji={snap.phase.emoji} worker_linked={snap.worker_linked} "
            f"pid_alive={snap.adapter.pid_alive} lang={snap.language}"
        )
        _write_log(
            instance_dir,
            {
                "kind": "supervisor_event_snapshot",
                "ts": now.isoformat(),
                "event_id": snap.event.id,
                "brain": snap.brain_spec,
                "age_seconds": round(snap.age_seconds, 1),
                "phase": snap.phase.phase,
                "emoji": snap.phase.emoji,
                "worker_linked": snap.worker_linked,
                "pid_alive": snap.adapter.pid_alive,
                "language": snap.language,
            },
        )

    if not dry_run:
        active_ids = {s.event.id for s in snapshots}
        state.prune(active_ids)
        state.last_tick_at = now.timestamp()
        state.save(instance_dir)

    _write_log(
        instance_dir,
        {
            "kind": "supervisor_tick_end",
            "ts": now.isoformat(),
            "qualifying": len(snapshots),
        },
    )
    return result


def _write_log(instance_dir: Path, record: dict) -> None:
    log_path = instance_dir / "state" / "logs" / "supervisor.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        pass
