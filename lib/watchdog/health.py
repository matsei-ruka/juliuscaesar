"""Health probes — pid_alive, cwd_match, proc_match, heartbeat_file."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .child import ChildSpec, ChildState


def pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_pidfile(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        return None
    if not raw:
        return None
    try:
        return int(raw.splitlines()[0])
    except ValueError:
        return None


def proc_cwd(pid: int) -> Path | None:
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    except (OSError, FileNotFoundError):
        return None


def proc_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except (OSError, FileNotFoundError):
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def find_pids_matching(pattern: str, *, cwd_match: Path | None = None) -> list[int]:
    """Scan /proc for pids whose cmdline matches `pattern`. Optional cwd filter."""
    rx = re.compile(pattern)
    pids: list[int] = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return pids
    cwd_resolved = cwd_match.resolve() if cwd_match else None
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        cmdline = proc_cmdline(pid)
        if not cmdline or not rx.search(cmdline):
            continue
        if cwd_resolved is not None:
            cwd = proc_cwd(pid)
            if cwd is None or cwd.resolve() != cwd_resolved:
                continue
        pids.append(pid)
    return pids


def heartbeat_fresh(path: Path, max_age: int) -> bool:
    try:
        mtime = path.stat().st_mtime
    except (OSError, FileNotFoundError):
        return False
    return (time.time() - mtime) <= max_age


def check(
    spec: ChildSpec,
    state: ChildState,
    *,
    instance_dir: Path,
    now: float | None = None,
) -> tuple[bool, str]:
    """Run every declared probe. Returns (alive, reason_if_dead)."""
    now = now or time.time()
    reasons: list[str] = []

    pid: int | None = None
    if spec.pidfile:
        pid = read_pidfile(_resolve_path(spec.pidfile, instance_dir))
    elif state.last_pid:
        pid = state.last_pid

    if spec.health.pid_alive:
        if not pid_alive(pid):
            return False, f"pidfile {spec.pidfile} → pid {pid} not alive"

    if spec.health.cwd_match:
        target = _resolve_path(spec.health.cwd_match, instance_dir).resolve()
        if pid is not None and pid_alive(pid):
            cwd = proc_cwd(pid)
            if cwd is None or cwd.resolve() != target:
                return False, f"cwd mismatch pid={pid} cwd={cwd} expected={target}"
        elif spec.health.proc_match:
            # cwd_match without a pidfile + with proc_match: use the regex
            # search to find a candidate, then enforce cwd.
            pids = find_pids_matching(spec.health.proc_match, cwd_match=target)
            if not pids:
                return False, f"no process matching {spec.health.proc_match!r} with cwd={target}"
            pid = pids[0]
        else:
            # cwd_match declared but we have no way to find a pid.
            return False, f"cwd_match declared but no pid available"

    if spec.health.proc_match and not spec.health.cwd_match:
        pids = find_pids_matching(spec.health.proc_match)
        if not pids:
            return False, f"no process matching {spec.health.proc_match!r}"
        pid = pid or pids[0]

    if spec.health.heartbeat_file:
        # Suppress heartbeat staleness within the start-grace window so a
        # daemon that hasn't booted yet is not classified as wedged.
        grace = spec.restart.start_grace_seconds
        if state.last_started_at and (now - state.last_started_at) < grace:
            pass
        else:
            hb_path = _resolve_path(spec.health.heartbeat_file, instance_dir)
            if not heartbeat_fresh(hb_path, spec.health.heartbeat_max_age_seconds):
                return False, f"heartbeat stale ({hb_path}, max_age={spec.health.heartbeat_max_age_seconds}s)"

    if pid is not None:
        state.last_pid = pid
    return True, ""


def _resolve_path(value: str, instance_dir: Path) -> Path:
    expanded = os.path.expandvars(value).replace("$INSTANCE_DIR", str(instance_dir))
    p = Path(expanded)
    if not p.is_absolute():
        p = instance_dir / p
    return p
