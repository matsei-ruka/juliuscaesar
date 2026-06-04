"""PID-liveness + log-pattern diagnostics for `jc doctor`.

See ``docs/specs/doctor-pid-liveness.md``.

Each helper returns a :class:`Finding` (or ``None`` when there is nothing
to say). ``bin/jc-doctor`` invokes them via a Python embed and parses the
``OK:`` / ``WARN:`` / ``FAIL:`` / ``INFO:`` prefixes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    level: str  # "ok" | "warn" | "info" | "fail"
    message: str


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_cmdline(pid: int) -> str | None:
    """Return /proc/<pid>/cmdline as a single space-joined string.

    None means the cmdline could not be read (Linux-only /proc absent, or
    process exited mid-read). Empty string means cmdline was present but
    blank (kernel threads).
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        # /proc absent (non-Linux) or process gone between os.kill and read.
        if not Path("/proc").exists():
            return None
        return ""
    except PermissionError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def _pid_finding(
    pidfile: Path,
    *,
    label: str,
    cmdline_markers: "list[str]",
) -> Finding:
    if not pidfile.exists():
        return Finding("info", f"{label} pidfile absent ({pidfile})")
    try:
        raw = pidfile.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return Finding("fail", f"{label} pidfile unreadable: {exc}")
    if not raw:
        return Finding("fail", f"{label} pidfile is empty ({pidfile})")
    try:
        pid = int(raw)
    except ValueError:
        return Finding("fail", f"{label} pidfile corrupt: {raw!r}")
    if not _pid_alive(pid):
        return Finding(
            "fail",
            f"{label} pidfile present but PID {pid} is dead "
            f"(was the daemon stopped without removing {pidfile.name}?)",
        )
    cmdline = _read_cmdline(pid)
    if cmdline is None:
        # Can't introspect (no /proc); trust the kill(0) signal.
        return Finding("ok", f"{label} running (pid {pid}, cmdline unverifiable)")
    if not any(m in cmdline for m in cmdline_markers):
        marker_repr = " or ".join(repr(m) for m in cmdline_markers)
        return Finding(
            "fail",
            f"{label} PID {pid} belongs to a different process "
            f"(cmdline missing {marker_repr})",
        )
    return Finding("ok", f"{label} running (pid {pid})")


def gateway_pid_finding(instance_dir: Path) -> Finding:
    pidfile = instance_dir / "state" / "gateway" / "jc-gateway.pid"
    return _pid_finding(pidfile, label="gateway daemon", cmdline_markers=["jc-gateway"])


def supervisor_pid_finding(instance_dir: Path) -> Finding:
    pidfile = instance_dir / "state" / "supervisor" / "jc-supervisor.pid"
    # The watch worker runs via `exec python3 - <instance_dir> <interval>` so its
    # cmdline won't contain "jc-supervisor" — accept the instance path as fallback.
    return _pid_finding(
        pidfile,
        label="supervisor daemon",
        cmdline_markers=["jc-supervisor", str(instance_dir)],
    )


_409_NEEDLE = "http error 409"


def telegram_409_finding(
    instance_dir: Path,
    *,
    tail_lines: int = 200,
) -> Finding | None:
    """Scan recent gateway log for cross-instance Telegram 409 conflicts.

    Returns None when the log is missing or contains no match — silence is
    the success case. WARN when at least one 409 line is present.
    """
    log_path = instance_dir / "state" / "gateway" / "gateway.log"
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()[-tail_lines:]
    matches = [line for line in lines if _409_NEEDLE in line.lower()]
    if not matches:
        return None
    last = matches[-1].strip()
    # Trim the line to keep doctor output scannable; only the prefix matters.
    if len(last) > 160:
        last = last[:157] + "..."
    return Finding(
        "warn",
        f"cross-instance bot-token contention detected — sibling instance "
        f"polling same bot ({len(matches)} 409 line(s); latest: {last})",
    )


def all_liveness_findings(instance_dir: Path) -> list[Finding]:
    """Convenience for `jc doctor` — returns every applicable finding."""
    out: list[Finding] = [
        gateway_pid_finding(instance_dir),
        supervisor_pid_finding(instance_dir),
    ]
    extra = telegram_409_finding(instance_dir)
    if extra is not None:
        out.append(extra)
    return out
