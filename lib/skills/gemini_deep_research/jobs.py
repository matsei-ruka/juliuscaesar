"""Async job state for deep-research runs.

A job is one detached `jc research run` invocation. The state file lives
at `<instance>/state/research/jobs/<job_id>.json` and records pid /
status / start time / output paths so `jc research status|result|cancel`
can inspect runs across CLI invocations.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


JOBS_SUBDIR = Path("state") / "research" / "jobs"
RESULTS_SUBDIR = Path("state") / "research"
EVENTS_SUBDIR = Path("state") / "events"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_job_id() -> str:
    """Sortable, lowercase ULID-ish: <unix-ts-hex>-<6 hex>."""
    return f"{int(time.time()):010x}-{secrets.token_hex(3)}"


@dataclass
class JobState:
    job_id: str
    query: str
    status: str = "running"
    pid: int | None = None
    started_at: str = field(default_factory=_utc_iso)
    finished_at: str | None = None
    duration_seconds: float | None = None
    exit_code: int | None = None
    message: str | None = None
    report_path: str | None = None
    meta_path: str | None = None
    sources_count: int | None = None
    backend: str = "gemini"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def jobs_dir(instance: Path) -> Path:
    path = instance / JOBS_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_state_path(instance: Path, job_id: str) -> Path:
    return jobs_dir(instance) / f"{job_id}.json"


def result_dir(instance: Path, job_id: str) -> Path:
    path = instance / RESULTS_SUBDIR / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def events_dir(instance: Path) -> Path:
    path = instance / EVENTS_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_state(instance: Path, state: JobState) -> Path:
    path = job_state_path(instance, state.job_id)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def read_state(instance: Path, job_id: str) -> JobState | None:
    path = job_state_path(instance, job_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return JobState(
        job_id=str(data.get("job_id") or job_id),
        query=str(data.get("query") or ""),
        status=str(data.get("status") or "unknown"),
        pid=data.get("pid") if isinstance(data.get("pid"), int) else None,
        started_at=str(data.get("started_at") or _utc_iso()),
        finished_at=data.get("finished_at"),
        duration_seconds=data.get("duration_seconds"),
        exit_code=data.get("exit_code"),
        message=data.get("message"),
        report_path=data.get("report_path"),
        meta_path=data.get("meta_path"),
        sources_count=data.get("sources_count"),
        backend=str(data.get("backend") or "gemini"),
    )


def list_jobs(instance: Path, *, limit: int | None = None) -> list[JobState]:
    out: list[JobState] = []
    base = instance / JOBS_SUBDIR
    if not base.exists():
        return out
    files = sorted(base.glob("*.json"), reverse=True)
    if limit is not None:
        files = files[:limit]
    for f in files:
        s = read_state(instance, f.stem)
        if s is not None:
            out.append(s)
    return out


def is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reconcile_status(state: JobState) -> JobState:
    """If the recorded pid is gone but state still says `running`, mark stale."""
    if state.status == "running" and state.pid and not is_pid_alive(state.pid):
        state.status = "stale"
        state.finished_at = state.finished_at or _utc_iso()
        state.message = state.message or "process disappeared"
    return state


def cancel(instance: Path, job_id: str) -> tuple[bool, str]:
    state = read_state(instance, job_id)
    if state is None:
        return False, "no such job"
    if state.status != "running" or not state.pid:
        return False, f"job is {state.status}; nothing to cancel"
    try:
        os.kill(state.pid, signal.SIGTERM)
    except ProcessLookupError:
        state.status = "stale"
        write_state(instance, state)
        return False, "process already gone"
    except PermissionError as exc:
        return False, f"cannot signal pid {state.pid}: {exc}"
    state.status = "cancelled"
    state.finished_at = _utc_iso()
    state.exit_code = -int(signal.SIGTERM)
    write_state(instance, state)
    return True, f"sent SIGTERM to pid {state.pid}"


def emit_completion_event(
    instance: Path,
    state: JobState,
    *,
    notify_chat_id: str | None,
    notify_channel: str = "telegram",
) -> Path:
    """Drop a `state/events/research-<id>.json` file for the jc-events channel."""
    payload: dict[str, Any] = {
        "event_id": f"research-{state.job_id}",
        "event_type": "research.completed",
        "job_id": state.job_id,
        "query": state.query,
        "status": "ok" if state.exit_code == 0 else "failed",
        "exit_code": state.exit_code,
        "duration_seconds": state.duration_seconds,
        "sources_count": state.sources_count,
        "report_path": state.report_path,
        "meta_path": state.meta_path,
        "backend": state.backend,
    }
    if notify_chat_id:
        payload["notify_chat_id"] = str(notify_chat_id)
        payload["notify_channel"] = notify_channel
    out = events_dir(instance) / f"research-{state.job_id}.json"
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(out)
    return out


__all__ = [
    "JobState",
    "JOBS_SUBDIR",
    "RESULTS_SUBDIR",
    "EVENTS_SUBDIR",
    "new_job_id",
    "jobs_dir",
    "job_state_path",
    "result_dir",
    "events_dir",
    "write_state",
    "read_state",
    "list_jobs",
    "is_pid_alive",
    "reconcile_status",
    "cancel",
    "emit_completion_event",
]
