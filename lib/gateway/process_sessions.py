"""Process session tracking to prevent orphan brain/adapter processes.

Tracks active gateway runtimes (gateway process PIDs, associated brain process,
start time, last heartbeat) in a JSON file. Used by watchdog to detect and
clean up stale or crashed processes.

Session file: `<instance>/state/gateway/process_sessions.json`
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass
class ProcessSession:
    """Active gateway runtime session."""

    session_id: str
    gateway_pid: int
    brain_pid: int | None
    brain_type: str
    adapter: str
    started_at: str
    last_heartbeat: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_alive(self) -> bool:
        """Check if gateway process is still alive."""
        try:
            os.kill(self.gateway_pid, 0)  # Signal 0: check existence
            return True
        except (OSError, ProcessLookupError):
            return False

    def is_stale(self, seconds: int = 3600) -> bool:
        """Check if heartbeat is older than N seconds."""
        hb = datetime.fromisoformat(self.last_heartbeat.replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        return hb < cutoff


def now_iso() -> str:
    """Current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sessions_path(instance_dir: Path) -> Path:
    return instance_dir / "state" / "gateway" / "process_sessions.json"


def _load_sessions(instance_dir: Path) -> list[ProcessSession]:
    """Load all process sessions from disk. Returns empty list if file missing."""
    path = _sessions_path(instance_dir)
    if not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return [ProcessSession(**s) for s in data.get("sessions", [])]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


def _save_sessions(instance_dir: Path, sessions: list[ProcessSession]) -> None:
    """Write process sessions to disk."""
    path = _sessions_path(instance_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"sessions": [s.to_dict() for s in sessions]}
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def register_session(
    instance_dir: Path,
    session_id: str,
    gateway_pid: int,
    brain_pid: int | None,
    brain_type: str,
    adapter: str,
    metadata: dict[str, Any] | None = None,
) -> ProcessSession:
    """Register a new process session."""
    sessions = _load_sessions(instance_dir)
    # Remove any existing session with same gateway_pid (cleanup stale entry)
    sessions = [s for s in sessions if s.gateway_pid != gateway_pid]

    session = ProcessSession(
        session_id=session_id,
        gateway_pid=gateway_pid,
        brain_pid=brain_pid,
        brain_type=brain_type,
        adapter=adapter,
        started_at=now_iso(),
        last_heartbeat=now_iso(),
        metadata=metadata or {},
    )
    sessions.append(session)
    _save_sessions(instance_dir, sessions)
    return session


def update_heartbeat(instance_dir: Path, session_id: str) -> None:
    """Update last_heartbeat for a session."""
    sessions = _load_sessions(instance_dir)
    for s in sessions:
        if s.session_id == session_id:
            s.last_heartbeat = now_iso()
            break
    _save_sessions(instance_dir, sessions)


def get_session(instance_dir: Path, session_id: str) -> ProcessSession | None:
    """Get a process session by ID."""
    sessions = _load_sessions(instance_dir)
    for s in sessions:
        if s.session_id == session_id:
            return s
    return None


def list_sessions(instance_dir: Path, alive_only: bool = False) -> list[ProcessSession]:
    """List all process sessions. Optionally filter to alive ones."""
    sessions = _load_sessions(instance_dir)
    if alive_only:
        sessions = [s for s in sessions if s.is_alive()]
    return sessions


def unregister_session(instance_dir: Path, session_id: str) -> None:
    """Remove a session from the registry."""
    sessions = _load_sessions(instance_dir)
    sessions = [s for s in sessions if s.session_id != session_id]
    _save_sessions(instance_dir, sessions)


def find_orphan_brains(instance_dir: Path, stale_after_seconds: int = 3600) -> list[int]:
    """Find brain PIDs that are orphaned (no active gateway session or stale).

    Returns list of PIDs to clean up.
    """
    sessions = list_sessions(instance_dir)
    active_brain_pids = set()

    for s in sessions:
        if not s.is_alive():
            continue
        if s.is_stale(stale_after_seconds):
            continue
        if s.brain_pid:
            active_brain_pids.add(s.brain_pid)

    # Find all brain processes and check if they're active
    orphans = []
    for s in sessions:
        if s.brain_pid and s.brain_pid not in active_brain_pids:
            # Check if the process is actually alive
            try:
                os.kill(s.brain_pid, 0)
                orphans.append(s.brain_pid)
            except (OSError, ProcessLookupError):
                pass
    return orphans


def kill_process(pid: int, timeout: float = 5.0) -> bool:
    """Kill a process gracefully (SIGTERM, then SIGKILL if needed).

    Returns True if killed, False if already dead or could not kill.
    """
    try:
        os.kill(pid, 0)  # Check if alive
    except (OSError, ProcessLookupError):
        return False  # Already dead

    try:
        os.kill(pid, 15)  # SIGTERM
        time.sleep(0.5)
        os.kill(pid, 0)  # Check again
        # Still alive, force kill
        time.sleep(timeout - 0.5)
        os.kill(pid, 9)  # SIGKILL
        return True
    except (OSError, ProcessLookupError):
        return True  # Successfully killed
