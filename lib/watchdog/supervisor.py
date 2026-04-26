"""Watchdog supervisor — main per-tick loop.

Cron invokes `python3 -m watchdog.supervisor <instance_dir>` (or via the bash
shim) every minute. The supervisor:

  1. Acquires a flock on `state/watchdog/lock` and exits if held.
  2. Loads `ops/watchdog.yaml`; for each enabled child:
     - Skip if alert_mode is set.
     - Run the child's health probes; on success, reset failure counters.
     - Otherwise, decide whether the restart budget allows a new attempt.
     - If yes, run the child's start command and persist updated state.
     - If no, flip alert_mode and emit a Telegram alert (best-effort).

State persists to `state/watchdog/state.json`.
"""

from __future__ import annotations

import errno
import fcntl
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from . import health, legacy_claude, policy
from .child import (
    ChildSpec,
    ChildState,
    StateStore,
    lock_path,
    log_dir,
    state_dir,
)
from .registry import load_enabled


AlertFn = Callable[[str, ChildSpec, ChildState], None]
LogFn = Callable[[str], None]


def acquire_lock(path: Path) -> int | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return None
        raise
    return fd


def release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _expand(value: str, instance_dir: Path) -> str:
    return os.path.expandvars(value).replace("$INSTANCE_DIR", str(instance_dir))


def _start_daemon(spec: ChildSpec, instance_dir: Path, log_file: Path) -> tuple[int, str]:
    if not spec.start:
        return 1, "no start command configured"
    cmd = _expand(spec.start, instance_dir)
    argv = shlex.split(cmd)
    if argv and not Path(argv[0]).exists():
        replacement = shutil.which(Path(argv[0]).name)
        if replacement:
            argv[0] = replacement
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab") as handle:
        env = os.environ.copy()
        env.setdefault("JC_INSTANCE_DIR", str(instance_dir))
        proc = subprocess.run(
            argv,
            cwd=str(instance_dir),
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
            check=False,
        )
    return proc.returncode, cmd


def _emit_alert(
    instance_dir: Path,
    msg: str,
    log: LogFn,
) -> None:
    """Send a Telegram alert via heartbeat/lib/send_telegram.sh. Best-effort."""
    sender = (
        Path(__file__).resolve().parents[1] / "heartbeat" / "lib" / "send_telegram.sh"
    )
    if not sender.exists() or not os.access(sender, os.X_OK):
        log(f"alert: sender missing at {sender} — printing to log: {msg}")
        return
    env = os.environ.copy()
    env["JC_INSTANCE_DIR"] = str(instance_dir)
    try:
        subprocess.run(
            ["bash", str(sender)],
            input=msg,
            text=True,
            env=env,
            timeout=15,
            check=False,
            capture_output=True,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"alert: send failed: {exc}")


class Supervisor:
    def __init__(
        self,
        instance_dir: Path,
        *,
        alert_fn: AlertFn | None = None,
        clock: Callable[[], float] | None = None,
        log: LogFn | None = None,
    ):
        self.instance_dir = instance_dir.resolve()
        self.clock = clock or time.time
        self.alert_fn = alert_fn or self._default_alert
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.state = StateStore(self.instance_dir)
        self._main_log = state_dir(self.instance_dir) / "supervisor.log"

    # --- entrypoints -------------------------------------------------------

    def run_tick(self) -> int:
        """One supervision tick — intended for cron. Returns process rc."""
        lock_fd = acquire_lock(lock_path(self.instance_dir))
        if lock_fd is None:
            self._log_supervisor("tick skipped — lock held")
            return 0
        try:
            self._do_tick()
        finally:
            release_lock(lock_fd)
        return 0

    def _do_tick(self) -> None:
        children = load_enabled(self.instance_dir)
        if not children:
            self._log_supervisor("no enabled children in ops/watchdog.yaml")
            self.state.save()
            return
        for spec in children:
            self._supervise_one(spec)
        self.state.save()

    # --- per-child ---------------------------------------------------------

    def _supervise_one(self, spec: ChildSpec) -> None:
        st = self.state.get(spec.name)
        if st.alert_mode:
            self._log_supervisor(f"{spec.name}: alert_mode — waiting for `jc watchdog reset`")
            return
        if spec.type == "legacy-claude":
            self._log_supervisor(
                f"{spec.name}: DEPRECATION legacy-claude child type — slated for removal in 0.5.0"
            )
        now = self.clock()
        alive, reason = health.check(spec, st, instance_dir=self.instance_dir, now=now)
        if alive:
            if st.consecutive_failures > 0:
                self._log_supervisor(f"{spec.name}: recovered after {st.consecutive_failures} failures")
            policy.record_healthy(st)
            st.last_failure = ""
            return
        st.last_failure = reason
        self._log_supervisor(f"{spec.name}: down — {reason}")
        if not policy.may_restart(spec, st, now=now):
            st.alert_mode = True
            attempts = len(st.attempts_in_window)
            window_min = max(1, int(spec.restart.window_seconds / 60))
            msg = (
                f"⚠️ {spec.name} restart loop — {attempts} restarts in {window_min}m"
                f"\nlast failure: {reason}"
                f"\nRun `jc watchdog reset {spec.name}` to clear and resume."
            )
            self._log_supervisor(f"{spec.name}: alert mode triggered ({attempts} attempts in window)")
            try:
                self.alert_fn(msg, spec, st)
            except Exception as exc:  # noqa: BLE001
                self._log_supervisor(f"{spec.name}: alert send failed: {exc}")
            return
        if not policy.attempt_due(spec, st, now=now):
            self._log_supervisor(
                f"{spec.name}: backoff — next attempt in "
                f"{int(policy.backoff_for(spec, st) - (now - st.last_attempt_at))}s"
            )
            return
        self._restart(spec, st, now=now)

    def _restart(self, spec: ChildSpec, st: ChildState, *, now: float) -> None:
        log_file = log_dir(self.instance_dir) / f"{spec.name}.log"
        if spec.type == "legacy-claude":
            rc, cmd = legacy_claude.restart(spec, self.instance_dir, log_file)
        elif spec.type == "daemon":
            rc, cmd = _start_daemon(spec, self.instance_dir, log_file)
        elif spec.type == "http-daemon":
            self._log_supervisor(f"{spec.name}: http-daemon type not implemented in v1")
            rc, cmd = 1, "(http-daemon unsupported in v1)"
        else:
            self._log_supervisor(f"{spec.name}: unknown child type {spec.type!r}")
            rc, cmd = 1, f"(unknown type {spec.type})"
        policy.record_attempt(st, now=now)
        if rc == 0:
            policy.record_started(st, now=now)
            self._log_supervisor(
                f"{spec.name}: restart attempt {st.consecutive_failures} → success ({cmd})"
            )
        else:
            self._log_supervisor(
                f"{spec.name}: restart attempt {st.consecutive_failures} → rc={rc} ({cmd})"
            )

    # --- alerting ----------------------------------------------------------

    def _default_alert(self, msg: str, spec: ChildSpec, st: ChildState) -> None:
        _emit_alert(self.instance_dir, msg, self._log_supervisor)

    # --- logging -----------------------------------------------------------

    def _log_supervisor(self, message: str) -> None:
        self._main_log.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with self._main_log.open("a", encoding="utf-8") as handle:
            handle.write(f"[{ts}] {message}\n")
        self.log(message)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: supervisor.py <instance_dir>", file=sys.stderr)
        return 2
    instance = Path(argv[0]).expanduser()
    if not instance.is_dir():
        print(f"instance dir does not exist: {instance}", file=sys.stderr)
        return 2
    sup = Supervisor(instance)
    return sup.run_tick()


if __name__ == "__main__":
    raise SystemExit(main())
