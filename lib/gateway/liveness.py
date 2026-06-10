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


# ───────────────────────── ownership checks (audit feature 9) ─────────────────


def _pid_uid(pid: int) -> int | None:
    """Real uid of a process via /proc/<pid>/status. None when unreadable."""
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    return None
    return None


def gateway_uid_finding(instance_dir: Path) -> Finding | None:
    """FAIL when the gateway process runs as a different uid than the
    instance owner — the root-contamination signature (a root crontab
    respawned the gateway as root; codex/claude sandboxes then can't reach
    /home/<jc_user> and state files become root-owned). Audit E-P1.
    """
    pidfile = instance_dir / "state" / "gateway" / "jc-gateway.pid"
    if not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if not _pid_alive(pid):
        return None  # covered by gateway_pid_finding
    proc_uid = _pid_uid(pid)
    if proc_uid is None:
        return Finding("info", "gateway process uid unverifiable (no /proc)")
    try:
        owner_uid = instance_dir.stat().st_uid
    except OSError:
        return None
    if proc_uid != owner_uid:
        return Finding(
            "fail",
            f"gateway pid {pid} runs as uid {proc_uid} but instance is owned "
            f"by uid {owner_uid} — root-contamination signature (check for a "
            "JC-WATCHDOG block in the wrong crontab)",
        )
    return Finding("ok", f"gateway process uid matches instance owner ({owner_uid})")


_STATE_OWNERSHIP_DIRS = ("state/gateway", "state/queue", "state/watchdog")


def state_ownership_findings(instance_dir: Path) -> list[Finding]:
    """FAIL per state dir containing entries owned by a foreign uid.

    Root-owned state files silently break the jc-user supervisor with
    PermissionError on its next tick (observed 2026-06-05, 3 hosts).
    """
    try:
        owner_uid = instance_dir.stat().st_uid
    except OSError:
        return []
    findings: list[Finding] = []
    for rel in _STATE_OWNERSHIP_DIRS:
        root = instance_dir / rel
        if not root.exists():
            continue
        foreign: list[str] = []
        try:
            entries = [root, *root.iterdir()]
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.stat().st_uid != owner_uid:
                    foreign.append(entry.name or str(entry))
            except OSError:
                continue
        if foreign:
            shown = ", ".join(sorted(foreign)[:5])
            findings.append(
                Finding(
                    "fail",
                    f"{rel} contains entries owned by a foreign uid: {shown}"
                    f"{' …' if len(foreign) > 5 else ''} — supervisor will hit "
                    "PermissionError; chown back to the instance owner",
                )
            )
    if not findings:
        return [Finding("ok", "state/ ownership matches instance owner")]
    return findings


def root_crontab_finding(instance_dir: Path) -> Finding | None:
    """Check root's crontab for a JC-WATCHDOG block naming this instance.

    Only conclusive when we can actually read root's crontab (running as
    root). Permission denied → INFO, never a false green. Audit E-P1: the
    2026-06-05 3-host contamination had zero framework guards.
    """
    import subprocess

    if os.geteuid() == 0:
        cmd = ["crontab", "-l"] if instance_dir.stat().st_uid == 0 else ["crontab", "-u", "root", "-l"]
    else:
        cmd = ["crontab", "-u", "root", "-l"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        stderr = (proc.stderr or "").lower()
        if "no crontab for" in stderr:
            return Finding("ok", "root crontab has no JC-WATCHDOG block")
        if os.geteuid() != 0:
            return Finding(
                "info",
                "cannot read root crontab (run doctor as root to verify "
                "no JC-WATCHDOG block landed there)",
            )
        return None
    lines = proc.stdout.splitlines()
    hits = [
        line
        for line in lines
        if "JC-WATCHDOG" in line or (str(instance_dir) in line and "watchdog" in line)
    ]
    if not hits:
        return Finding("ok", "root crontab has no JC-WATCHDOG block")
    if instance_dir.stat().st_uid == 0:
        return Finding("ok", "JC-WATCHDOG in root crontab (instance is root-owned)")
    return Finding(
        "fail",
        f"JC-WATCHDOG block found in ROOT's crontab ({len(hits)} line(s)) but "
        "the instance is not root-owned — remove it and reinstall via "
        "`su - <jc_user> -c` (root-contamination class, 2026-06-05)",
    )


def ownership_findings(instance_dir: Path) -> list[Finding]:
    out: list[Finding] = []
    g = gateway_uid_finding(instance_dir)
    if g is not None:
        out.append(g)
    out.extend(state_ownership_findings(instance_dir))
    r = root_crontab_finding(instance_dir)
    if r is not None:
        out.append(r)
    return out
