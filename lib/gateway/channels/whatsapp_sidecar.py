"""WhatsApp sidecar process lifecycle.

Starts, stops, and supervises the Node.js sidecar subprocess. The protocol
is stdio JSON lines:

    sidecar stdout → Python reads inbound events (qr, connection, message, send_result, error)
    sidecar stdin  ← Python writes outbound commands (send, stop)
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import whatsapp_protocol as protocol

NODE_BIN = "node"
SIDECAR_SCRIPT = "dist/index.js"
RESTART_BACKOFF = (5, 30, 120)  # seconds between restart attempts


class SidecarError(Exception):
    """Raised when the sidecar fails fatally (e.g., auth_missing)."""


def _sidecar_dir() -> Path:
    """Return the sidecar directory within the framework checkout."""
    return Path(__file__).resolve().parent / "whatsapp_sidecar"


class WhatsAppSidecar:
    """Manages the lifecycle of the Node.js sidecar process.

    Usage::

        sidecar = WhatsAppSidecar(auth_dir, account_id, callbacks)
        sidecar.start()
        # ... sidecar emits events via stdout_callback ...
        sidecar.send(jid, text)
        sidecar.stop()
    """

    def __init__(
        self,
        auth_dir: str,
        account_id: str = "default",
        *,
        on_event: Callable[[dict[str, Any]], None],
        on_log: Callable[[str], None],
    ):
        self.auth_dir = auth_dir
        self.account_id = account_id
        self._on_event = on_event
        self._on_log = on_log
        self._proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._restart_count = 0
        self._restart_until: float = 0
        self._lock = threading.Lock()

    # ── Public API ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the sidecar subprocess. Blocks briefly while it boots."""
        self._spawn()

    def stop(self) -> None:
        """Gracefully shut down the sidecar."""
        self._stop_event.set()
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            try:
                # Send stop command
                cmd = protocol.StopCommand()
                proc.stdin.write(protocol.encode_command(cmd) + "\n")
                proc.stdin.flush()
            except (OSError, BrokenPipeError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def send(self, to: str, text: str, quoted_message_id: str | None = None) -> str:
        """Send a text message. Returns a command id for correlating the send_result."""
        cmd_id = f"send_{int(time.monotonic() * 1000)}"
        cmd = protocol.SendCommand(
            type="send",
            id=cmd_id,
            to=to,
            text=text,
            quoted_message_id=quoted_message_id,
        )
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            raise SidecarError("sidecar not running")
        try:
            proc.stdin.write(protocol.encode_command(cmd) + "\n")
            proc.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise SidecarError(f"sidecar stdin write failed: {exc}") from exc
        return cmd_id

    def download(self, message_key: dict, dest_path: str) -> str:
        """Download media from a WhatsApp message. Returns a command id."""
        cmd_id = f"dl_{int(time.monotonic() * 1000)}"
        cmd = protocol.DownloadCommand(
            type="download",
            id=cmd_id,
            message_key=message_key,
            dest_path=dest_path,
        )
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            raise SidecarError("sidecar not running")
        try:
            proc.stdin.write(protocol.encode_command(cmd) + "\n")
            proc.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise SidecarError(f"sidecar stdin write failed: {exc}") from exc
        return cmd_id

    # ── Internal ────────────────────────────────────────────────────────

    def _spawn(self) -> None:
        sidecar_root = _sidecar_dir()
        entry = sidecar_root / SIDECAR_SCRIPT

        if not entry.exists():
            raise SidecarError(
                f"sidecar not found at {entry}. "
                f"Run 'npm install && npm run build' in {sidecar_root}"
            )

        env = os.environ.copy()
        # Suppress pino/baileys debug noise; keep warnings
        env.setdefault("NODE_ENV", "production")

        self._on_log(f"starting sidecar: {entry} --auth-dir {self.auth_dir}")

        with self._lock:
            self._proc = subprocess.Popen(
                [NODE_BIN, str(entry), "--auth-dir", self.auth_dir, "--account-id", self.account_id],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

        # Background thread to read stdout line by line
        self._stop_event.clear()
        self._reader_thread = threading.Thread(
            target=self._read_stdout, daemon=True
        )
        self._reader_thread.start()

        # Background thread to read stderr and log it
        stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True
        )
        stderr_thread.start()

    def _read_stdout(self) -> None:
        """Read JSON lines from sidecar stdout and dispatch to on_event."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                event = protocol.decode_event(line)
                if event.get("type") == "_parse_error":
                    self._on_log(f"sidecar stdout parse error: {event.get('error')}")
                    continue
                if event.get("type") == "_empty":
                    continue
                self._on_event(event)
        except (OSError, ValueError):
            pass
        finally:
            # Sidecar exited. Decide whether to restart.
            if not self._stop_event.is_set():
                self._handle_exit()

    def _read_stderr(self) -> None:
        """Read sidecar stderr and forward to on_log."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                self._on_log(f"[sidecar] {line.rstrip()}")
        except (OSError, ValueError):
            pass

    def _handle_exit(self) -> None:
        """Called when the sidecar process exits unexpectedly."""
        if self._stop_event.is_set():
            return

        now = time.monotonic()
        if now < self._restart_until:
            self._on_log("sidecar restart suppressed (backoff window)")
            return

        self._restart_count += 1
        backoff_idx = min(self._restart_count - 1, len(RESTART_BACKOFF) - 1)
        delay = RESTART_BACKOFF[backoff_idx]
        self._restart_until = now + delay

        self._on_log(
            f"sidecar exited (restart #{self._restart_count}, "
            f"backoff {delay}s)"
        )

        if self._restart_count > 3:
            self._on_event({
                "type": "error",
                "fatal": True,
                "reason": f"sidecar crashed {self._restart_count} times, giving up",
            })
            return

        time.sleep(delay)
        if not self._stop_event.is_set():
            self._spawn()
