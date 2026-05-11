"""Deep Research skill — Gemini-backed (v1).

Public Python API used by `bin/jc-research` and by in-process callers
(workers, heartbeat tasks). Browser plumbing lives in `runner.py`; state
files live in `jobs.py`; profile/lock primitives live in `auth.py`.

Top-level exports stay browser-free so `import` is cheap and unit tests
do not need Playwright installed. The runner lazy-imports its own deps.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .auth import (
    DEFAULT_LOCK_TIMEOUT,
    PROFILE_ENV_VAR,
    acquire_lock,
    ensure_profile_dir,
    profile_age_seconds,
    profile_dir,
    profile_exists,
)
from .errors import (
    CODE_LABELS,
    EXIT_AUTH_REQUIRED,
    EXIT_BROWSER_CRASH,
    EXIT_BUSY,
    EXIT_CAPTCHA,
    EXIT_DEEP_RESEARCH_UNAVAILABLE,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    EXIT_QUOTA,
    EXIT_SELECTORS_FAILED,
    DeepResearchError,
)
from .jobs import (
    JobState,
    cancel as cancel_job,
    emit_completion_event,
    is_pid_alive,
    job_state_path,
    list_jobs,
    new_job_id,
    read_state,
    reconcile_status,
    result_dir,
    write_state,
)


def run(
    query: str,
    *,
    instance_dir: Path,
    job_id: str | None = None,
    max_wait_seconds: int = 900,
    headed: bool = False,
    notify_chat_id: str | None = None,
    notify_channel: str = "telegram",
) -> JobState:
    """Run a query synchronously. Blocks until Gemini completes or fails.

    Always returns a `JobState`; inspect `state.exit_code` for the result.
    Writes the same files as `start()` so callers can treat the output the
    same way regardless of mode.
    """
    from .runner import RunInputs, run_query  # lazy: keep top-level import cheap

    job_id = job_id or new_job_id()
    instance = instance_dir.resolve()
    out_dir = result_dir(instance, job_id)
    state = JobState(
        job_id=job_id,
        query=query,
        status="running",
        pid=os.getpid(),
        backend="gemini",
    )
    write_state(instance, state)

    inputs = RunInputs(
        query=query,
        out_dir=out_dir,
        job_id=job_id,
        max_wait_seconds=max_wait_seconds,
        headed=headed,
    )
    started_at = time.monotonic()
    result = run_query(inputs)
    duration = time.monotonic() - started_at

    state.finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state.duration_seconds = result.duration_seconds or duration
    state.exit_code = result.exit_code
    state.message = result.message
    state.report_path = str(result.report_path) if result.report_path else None
    state.meta_path = str(result.meta_path) if result.meta_path else None
    state.sources_count = result.sources_count
    state.status = "ok" if result.exit_code == EXIT_OK else "failed"
    write_state(instance, state)
    emit_completion_event(
        instance,
        state,
        notify_chat_id=notify_chat_id,
        notify_channel=notify_channel,
    )
    return state


def start(
    query: str,
    *,
    instance_dir: Path,
    cli_path: str | None = None,
    max_wait_seconds: int = 900,
    headed: bool = False,
    notify_chat_id: str | None = None,
    notify_channel: str = "telegram",
) -> JobState:
    """Detach a background `jc-research run` for `query` and return immediately.

    The detached child writes to the same job state file, so `status()`
    sees its progress without any IPC.
    """
    if not query or not query.strip():
        raise DeepResearchError(EXIT_INVALID_INPUT, "query is empty")
    instance = instance_dir.resolve()
    job_id = new_job_id()
    state = JobState(job_id=job_id, query=query, status="running", backend="gemini")
    write_state(instance, state)

    cli = cli_path or _resolve_cli_path()
    cmd = [
        cli,
        "--instance-dir",
        str(instance),
        "run",
        "--job-id",
        job_id,
        "--max-wait",
        str(int(max_wait_seconds)),
        "--quiet",
    ]
    if headed:
        cmd.append("--debug")
    if notify_chat_id:
        cmd += ["--notify-chat-id", str(notify_chat_id)]
    if notify_channel:
        cmd += ["--notify-channel", notify_channel]
    cmd += ["--", query]

    log_dir = result_dir(instance, job_id)
    stdout = open(log_dir / "spawn.log", "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(instance),
            stdout=stdout,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except FileNotFoundError as exc:
        stdout.close()
        raise DeepResearchError(
            EXIT_INVALID_INPUT,
            f"jc-research CLI not found at {cli}. Is install.sh complete?",
        ) from exc
    state.pid = proc.pid
    write_state(instance, state)
    return state


def status(instance_dir: Path, job_id: str) -> JobState | None:
    state = read_state(instance_dir.resolve(), job_id)
    if state is None:
        return None
    return reconcile_status(state)


def result(instance_dir: Path, job_id: str) -> str | None:
    """Return the rendered report markdown, or None if missing."""
    state = read_state(instance_dir.resolve(), job_id)
    if state is None or not state.report_path:
        return None
    path = Path(state.report_path)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def cancel(instance_dir: Path, job_id: str) -> tuple[bool, str]:
    return cancel_job(instance_dir.resolve(), job_id)


def list_(instance_dir: Path, *, limit: int | None = None) -> list[JobState]:
    return [reconcile_status(s) for s in list_jobs(instance_dir.resolve(), limit=limit)]


def _resolve_cli_path() -> str:
    """Find `jc-research` on PATH; fall back to the shipped script."""
    import shutil as _shutil

    found = _shutil.which("jc-research")
    if found:
        return found
    here = Path(__file__).resolve().parents[3] / "bin" / "jc-research"
    if here.exists():
        return str(here)
    return "jc-research"


__all__ = [
    "DEFAULT_LOCK_TIMEOUT",
    "PROFILE_ENV_VAR",
    "CODE_LABELS",
    "EXIT_AUTH_REQUIRED",
    "EXIT_BROWSER_CRASH",
    "EXIT_BUSY",
    "EXIT_CAPTCHA",
    "EXIT_DEEP_RESEARCH_UNAVAILABLE",
    "EXIT_INVALID_INPUT",
    "EXIT_OK",
    "EXIT_QUOTA",
    "EXIT_SELECTORS_FAILED",
    "DeepResearchError",
    "JobState",
    "acquire_lock",
    "cancel",
    "ensure_profile_dir",
    "is_pid_alive",
    "list_",
    "new_job_id",
    "profile_age_seconds",
    "profile_dir",
    "profile_exists",
    "result",
    "run",
    "start",
    "status",
]
