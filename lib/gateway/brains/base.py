"""Shared infrastructure for per-brain wrappers.

Each brain module subclasses `Brain` and overrides:

  - `prompt_for_event` to build the input string
  - `capture_session_id` to read back the new native session id
  - optionally `extra_env` / `extra_args` for per-brain config
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .. import chats as chats_module
from ..config import BrainOverrideConfig, GatewayConfig
from ..context import render_preamble
from ..queue import Event


FRAMEWORK_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS_DIR = FRAMEWORK_ROOT / "heartbeat" / "adapters"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class BrainResult:
    response: str
    session_id: str | None = None


class AdapterFailure(RuntimeError):
    """Raised when an adapter exits non-zero.

    Carries the tail of stderr (capped) so the recovery classifier can route
    the failure without re-reading the gateway log. Subclass of RuntimeError
    for backwards compat with existing dispatcher catches.
    """

    def __init__(self, brain: str, rc: int, stderr_tail: str):
        super().__init__(f"adapter {brain} failed with exit {rc}")
        self.brain = brain
        self.rc = rc
        self.stderr_tail = stderr_tail or ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def newest_jsonl_stem(root: Path, since: float, *, recursive: bool = False) -> str | None:
    if not root.is_dir():
        return None
    files = root.rglob("*.jsonl") if recursive else root.glob("*.jsonl")
    best = None
    best_delta = None
    for path in files:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < since - 1:
            continue
        delta = abs(mtime - since)
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best = path
    return best.stem if best else None


def killpg(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


_STDERR_TAIL_LINES = 80
_STDERR_TAIL_BYTES = 8 * 1024


def _read_tail(path: Path) -> str:
    """Return the last 80 lines / 8KB of the adapter stderr file. Best-effort."""
    try:
        data = path.read_bytes()
    except (OSError, FileNotFoundError):
        return ""
    if len(data) > _STDERR_TAIL_BYTES:
        data = data[-_STDERR_TAIL_BYTES:]
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > _STDERR_TAIL_LINES:
        lines = lines[-_STDERR_TAIL_LINES:]
    return "\n".join(lines)


class Brain:
    name: str = ""
    needs_l1_preamble: bool = True

    def __init__(self, instance_dir: Path, *, override: BrainOverrideConfig | None = None):
        self.instance_dir = instance_dir
        self.override = override or BrainOverrideConfig()

    # --- subclass hooks ----------------------------------------------------

    def adapter_path(self) -> Path:
        return ADAPTERS_DIR / f"{self.name}.sh"

    def prompt_for_event(self, event: Event) -> str:
        meta = self._meta(event)
        meta_text = json.dumps(meta, indent=2, sort_keys=True) if meta else "{}"
        preamble = render_preamble(self.instance_dir) if self.needs_l1_preamble else ""
        if self.needs_l1_preamble and event.source == "telegram":
            chats_section = self._render_known_chats_section()
            if chats_section:
                preamble = f"{preamble}\n\n{chats_section}" if preamble else chats_section
        body = f"""{preamble}

# Incoming event

- id: {event.id}
- source: {event.source}
- user_id: {event.user_id or "-"}
- conversation_id: {event.conversation_id or "-"}
- metadata:
{meta_text}

# User message

{event.content}
"""
        return body

    def _render_known_chats_section(self, limit: int = 20) -> str:
        """Build the `## Known Telegram chats` block from the chats table.

        Returns "" when no chats are recorded yet (so empty preambles stay
        empty for fresh instances).
        """
        try:
            rows = chats_module.list_chats(
                self.instance_dir, channel="telegram", limit=limit
            )
        except Exception:  # noqa: BLE001
            return ""
        if not rows:
            return ""
        lines = ["## Known Telegram chats", ""]
        for chat in rows:
            ctype = chat.chat_type or "?"
            title = chat.title or "(untitled)"
            handle = f" (@{chat.username})" if chat.username else ""
            members = (
                f" ({chat.member_count} members)"
                if chat.member_count is not None
                else ""
            )
            last = (chat.last_seen or "")[:16].replace("T", " ")
            lines.append(
                f"- {chat.chat_id} | {ctype} | {title}{handle}{members} — last {last}"
            )
        return "\n".join(lines)

    def extra_env(self) -> dict[str, str]:
        return {}

    def capture_session_id(self, started_at: str) -> str | None:
        return None

    # --- invocation --------------------------------------------------------

    def validate(self) -> None:
        adapter = self.adapter_path()
        if not adapter.exists():
            raise FileNotFoundError(f"adapter not found: {adapter}")
        if not os.access(adapter, os.X_OK):
            raise PermissionError(f"adapter not executable: {adapter}")

    def invoke(
        self,
        *,
        event: Event,
        model: str | None,
        resume_session: str | None,
        timeout_seconds: int,
        log_path: Path,
        log_event: Callable[[str], None] | None = None,
    ) -> BrainResult:
        self.validate()
        prompt = self.prompt_for_event(event)
        env = os.environ.copy()
        env["JC_INSTANCE_DIR"] = str(self.instance_dir)
        if resume_session:
            env["JC_RESUME_SESSION"] = resume_session
            env["WORKER_RESUME_SESSION"] = resume_session
        else:
            env.pop("JC_RESUME_SESSION", None)
            env.pop("WORKER_RESUME_SESSION", None)
        env.update(self.extra_env())
        timeout = self.override.timeout_seconds or timeout_seconds
        start = now_iso()
        wall_start = time.monotonic()
        log = log_event or (lambda _msg: None)
        # Stderr goes to a per-invocation scratch file so we can extract a tail
        # for the recovery classifier on rc!=0. We append the full contents to
        # the gateway log on completion (success or failure) so log forensics
        # are unchanged.
        stderr_dir = self.instance_dir / "state" / "gateway" / "adapter_stderr"
        stderr_dir.mkdir(parents=True, exist_ok=True)
        stderr_path = stderr_dir / f"{event.id}-{os.getpid()}-{int(wall_start)}.log"
        with log_path.open("ab") as binlog:
            binlog.write(
                f"[{start}] adapter start event={event.id} brain={self.name} model={model or '-'}\n".encode()
            )
            # Flush + fsync the header so a SIGTERM-mid-call cannot lose the
            # only forensic marker that the adapter was about to spawn.
            # Caught a real bug 2026-04-26 where 8 KB block buffer ate the
            # "adapter start" line for events 73 + 74.
            binlog.flush()
            try:
                os.fsync(binlog.fileno())
            except OSError:
                pass
            stderr_handle = stderr_path.open("wb")
            try:
                try:
                    proc = subprocess.Popen(
                        [str(self.adapter_path()), model or ""],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=stderr_handle,
                        cwd=str(self.instance_dir),
                        env=env,
                        start_new_session=True,
                        text=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    log(
                        f"adapter spawn failed event={event.id} brain={self.name} reason={exc}"
                    )
                    raise
                log(
                    f"adapter spawn event={event.id} brain={self.name} pid={proc.pid} "
                    f"model={model or '-'} resume={'yes' if resume_session else 'no'}"
                )
                try:
                    stdout, _ = proc.communicate(prompt, timeout=timeout)
                except subprocess.TimeoutExpired:
                    duration = time.monotonic() - wall_start
                    log(
                        f"adapter timeout event={event.id} brain={self.name} "
                        f"pid={proc.pid} duration={duration:.1f}s timeout={timeout}s"
                    )
                    killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                    raise TimeoutError(f"adapter timeout after {timeout}s")
                duration = time.monotonic() - wall_start
                log(
                    f"adapter exit event={event.id} brain={self.name} pid={proc.pid} "
                    f"rc={proc.returncode} duration={duration:.1f}s"
                )
            finally:
                stderr_handle.close()
                try:
                    binlog.write(stderr_path.read_bytes())
                except OSError:
                    pass
            if proc.returncode != 0:
                raise AdapterFailure(self.name, proc.returncode, _read_tail(stderr_path))
        try:
            stderr_path.unlink()
        except OSError:
            pass
        session_id = None
        try:
            session_id = self.capture_session_id(start)
        except Exception:  # noqa: BLE001
            session_id = None
        return BrainResult(response=stdout.strip(), session_id=session_id)

    # --- helpers -----------------------------------------------------------

    def _meta(self, event: Event) -> dict:
        if not event.meta:
            return {}
        try:
            data = json.loads(event.meta)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
