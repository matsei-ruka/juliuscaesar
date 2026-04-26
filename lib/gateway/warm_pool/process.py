"""Persistent `claude -p` subprocess wrapper, single-turn-per-invoke.

Spawned once via `start()`; reads init event to confirm readiness. Each
`invoke(prompt, timeout)` writes one user message line, drains stdout events
until the terminal `result` event, returns an `InvokeResult`. Members are
single-threaded by contract — the pool guarantees no concurrent `invoke()` on
the same instance.

On any framing/IO error the member is marked unhealthy; the pool will evict
and respawn on the next get.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .protocol import (
    InvokeResult,
    encode_user_message,
    extract_result,
    is_terminal_event,
    parse_event_line,
)


class PoolProcessError(RuntimeError):
    """Raised when a pool member is unusable (spawn failed, dead, unhealthy)."""


@dataclass
class PoolProcess:
    claude_bin: str
    instance_dir: Path
    model: str | None
    resume_session: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    extra_args: tuple[str, ...] = ()
    startup_timeout_seconds: float = 30.0

    proc: subprocess.Popen | None = field(default=None, init=False, repr=False)
    session_id: str | None = field(default=None, init=False)
    created_at: float = field(default=0.0, init=False)
    last_used: float = field(default=0.0, init=False)
    message_count: int = field(default=0, init=False)
    healthy: bool = field(default=False, init=False)
    last_error: str | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _stderr_buf: list[str] = field(default_factory=list, init=False, repr=False)

    def start(self) -> None:
        if self.proc is not None:
            return
        args = self._build_args()
        env = os.environ.copy()
        env["JC_INSTANCE_DIR"] = str(self.instance_dir)
        if self.resume_session:
            env["JC_RESUME_SESSION"] = self.resume_session
            env["WORKER_RESUME_SESSION"] = self.resume_session
        env.update(self.extra_env)
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.instance_dir),
                env=env,
                start_new_session=True,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise PoolProcessError(f"claude spawn failed: {exc}") from exc
        self.proc = proc
        self.created_at = time.monotonic()
        self.last_used = self.created_at
        threading.Thread(
            target=self._drain_stderr, daemon=True, name="warmpool-stderr"
        ).start()
        try:
            self._await_init()
        except PoolProcessError:
            self.terminate()
            raise
        self.healthy = True

    def _build_args(self) -> list[str]:
        args = [
            self.claude_bin,
            "-p",
            "--input-format=stream-json",
            "--output-format=stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--strict-mcp-config",
        ]
        if self.model:
            args.extend(["--model", self.model])
        if self.resume_session:
            args.extend(["--resume", self.resume_session])
        args.extend(self.extra_args)
        return args

    def _drain_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                self._stderr_buf.append(line.rstrip("\n"))
                if len(self._stderr_buf) > 200:
                    self._stderr_buf = self._stderr_buf[-100:]
        except (ValueError, OSError):
            return

    def _await_init(self) -> None:
        deadline = time.monotonic() + self.startup_timeout_seconds
        while time.monotonic() < deadline:
            line = self._readline_with_deadline(deadline)
            if line is None:
                break
            evt = parse_event_line(line)
            if evt is None:
                continue
            if evt.get("type") == "system" and evt.get("subtype") == "init":
                sid = evt.get("session_id")
                if isinstance(sid, str):
                    self.session_id = sid
                return
            # Hooks may emit extra system events before init; keep reading.
        raise PoolProcessError(
            f"claude did not emit init within {self.startup_timeout_seconds}s; "
            f"stderr_tail={self._stderr_tail()}"
        )

    def _readline_with_deadline(self, deadline: float) -> str | None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return None
        # Popen text-mode stdout doesn't expose a portable per-read timeout,
        # so we rely on a watchdog thread that kills the process if the read
        # hangs past the deadline. For startup only — invoke() uses its own.
        timer: threading.Timer | None = None
        if deadline > time.monotonic():
            remaining = deadline - time.monotonic()
            timer = threading.Timer(remaining, self._kill_for_timeout)
            timer.daemon = True
            timer.start()
        try:
            line = proc.stdout.readline()
        except (ValueError, OSError):
            return None
        finally:
            if timer is not None:
                timer.cancel()
        if not line:
            return None
        return line

    def _kill_for_timeout(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def invoke(self, prompt: str, *, timeout_seconds: float) -> InvokeResult:
        proc = self.proc
        if proc is None or not self.healthy:
            raise PoolProcessError("invoke on unstarted/unhealthy member")
        if proc.poll() is not None:
            self.healthy = False
            raise PoolProcessError(f"claude exited rc={proc.returncode}")

        with self._lock:
            assert proc.stdin is not None and proc.stdout is not None
            line = encode_user_message(prompt) + "\n"
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self.healthy = False
                self.last_error = f"stdin write: {exc}"
                raise PoolProcessError(self.last_error) from exc

            events: list[dict] = []
            deadline = time.monotonic() + timeout_seconds
            timer = threading.Timer(timeout_seconds, self._kill_for_timeout)
            timer.daemon = True
            timer.start()
            try:
                while True:
                    if time.monotonic() >= deadline:
                        self.healthy = False
                        self.last_error = "invoke timeout"
                        raise PoolProcessError("invoke timeout")
                    try:
                        raw = proc.stdout.readline()
                    except (ValueError, OSError) as exc:
                        self.healthy = False
                        self.last_error = f"stdout read: {exc}"
                        raise PoolProcessError(self.last_error) from exc
                    if not raw:
                        self.healthy = False
                        self.last_error = (
                            f"claude EOF; rc={proc.poll()}; stderr_tail={self._stderr_tail()}"
                        )
                        raise PoolProcessError(self.last_error)
                    evt = parse_event_line(raw)
                    if evt is None:
                        continue
                    events.append(evt)
                    if is_terminal_event(evt):
                        break
            finally:
                timer.cancel()

            result = extract_result(events)
            if result.session_id:
                self.session_id = result.session_id
            self.last_used = time.monotonic()
            self.message_count += 1
            if result.is_error:
                # Don't mark unhealthy on a single api error — that's a per-turn
                # condition (rate limit, content policy). Pool eviction happens
                # only on transport failures.
                self.last_error = result.error_text
            return result

    def terminate(self, *, grace_seconds: float = 2.0) -> None:
        proc = self.proc
        self.healthy = False
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    proc.wait(timeout=grace_seconds)
                except subprocess.TimeoutExpired:
                    pass
        self.proc = None

    def is_alive(self) -> bool:
        proc = self.proc
        return proc is not None and proc.poll() is None

    def _stderr_tail(self, lines: int = 10) -> str:
        return " | ".join(self._stderr_buf[-lines:])
