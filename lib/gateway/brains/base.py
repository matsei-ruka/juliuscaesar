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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

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
        with log_path.open("ab") as log:
            log.write(
                f"[{start}] adapter start event={event.id} brain={self.name} model={model or '-'}\n".encode()
            )
            proc = subprocess.Popen(
                [str(self.adapter_path()), model or ""],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                cwd=str(self.instance_dir),
                env=env,
                start_new_session=True,
                text=True,
            )
            try:
                stdout, _ = proc.communicate(prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                raise TimeoutError(f"adapter timeout after {timeout}s")
            if proc.returncode != 0:
                raise RuntimeError(f"adapter {self.name} failed with exit {proc.returncode}")
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
