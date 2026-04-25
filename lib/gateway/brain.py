"""Brain adapter invocation for gateway events."""

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

from .config import SUPPORTED_BRAINS
from .queue import Event


FRAMEWORK_ROOT = Path(__file__).resolve().parents[1]
ADAPTERS_DIR = FRAMEWORK_ROOT / "heartbeat" / "adapters"
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


@dataclass(frozen=True)
class BrainResult:
    response: str
    session_id: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def build_prompt(instance_dir: Path, event: Event) -> str:
    meta = {}
    if event.meta:
        try:
            meta = json.loads(event.meta)
        except json.JSONDecodeError:
            meta = {}
    memory = []
    for name in ("IDENTITY.md", "USER.md", "RULES.md", "HOT.md"):
        path = instance_dir / "memory" / "L1" / name
        if path.exists():
            memory.append(f"## {name}\n{path.read_text(encoding='utf-8', errors='replace')[:8000]}")
    memory_text = "\n\n".join(memory) if memory else "(No L1 memory files found.)"
    meta_text = json.dumps(meta, indent=2, sort_keys=True) if meta else "{}"
    return f"""You are Julius, the assistant for this JuliusCaesar instance.

Use the instance memory and answer the user's channel message directly. Keep the
response appropriate for chat. Do not mention internal queue ids unless useful.

# Instance memory

{memory_text}

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


def adapter_path(brain: str) -> Path:
    return ADAPTERS_DIR / f"{brain}.sh"


def validate_brain(brain: str) -> None:
    if brain not in SUPPORTED_BRAINS:
        raise ValueError(f"unsupported brain: {brain}")
    adapter = adapter_path(brain)
    if not adapter.exists():
        raise FileNotFoundError(f"adapter not found: {adapter}")
    if not os.access(adapter, os.X_OK):
        raise PermissionError(f"adapter not executable: {adapter}")


def call_brain(
    *,
    instance_dir: Path,
    event: Event,
    brain: str,
    model: str | None,
    resume_session: str | None,
    timeout_seconds: int,
    log_path: Path,
) -> BrainResult:
    validate_brain(brain)
    prompt = build_prompt(instance_dir, event)
    env = os.environ.copy()
    env["JC_INSTANCE_DIR"] = str(instance_dir)
    if resume_session:
        env["JC_RESUME_SESSION"] = resume_session
        env["WORKER_RESUME_SESSION"] = resume_session
    else:
        env.pop("JC_RESUME_SESSION", None)
        env.pop("WORKER_RESUME_SESSION", None)
    start = now_iso()
    with log_path.open("ab") as log:
        log.write(
            f"[{start}] adapter start event={event.id} brain={brain} model={model or '-'}\n".encode()
        )
        proc = subprocess.Popen(
            [str(adapter_path(brain)), model or ""],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            cwd=str(instance_dir),
            env=env,
            start_new_session=True,
            text=True,
        )
        try:
            stdout, _ = proc.communicate(prompt, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise TimeoutError(f"adapter timeout after {timeout_seconds}s")
        if proc.returncode != 0:
            raise RuntimeError(f"adapter {brain} failed with exit {proc.returncode}")
    session_id = capture_session_id(brain, instance_dir, start)
    return BrainResult(response=stdout.strip(), session_id=session_id)


def _killpg(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def capture_session_id(brain: str, instance: Path, started_at: str) -> str | None:
    try:
        t0 = _parse_iso(started_at)
        if t0 is None:
            return None
        if brain == "claude":
            slug = str(instance).replace("/", "-").replace("_", "-")
            return _newest_jsonl_stem(Path.home() / ".claude" / "projects" / slug, t0)
        if brain == "codex":
            root = Path.home() / ".codex"
            root = root / "sessions" if (root / "sessions").is_dir() else root
            stem = _newest_jsonl_stem(root, t0, recursive=True)
            if not stem:
                return None
            match = _UUID_RE.search(stem)
            return match.group(0) if match else stem
        if brain == "gemini":
            proc = subprocess.run(
                ["gemini", "--list-sessions"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode != 0:
                return None
            matches = _UUID_RE.findall(proc.stdout)
            return matches[-1] if matches else None
        if brain == "opencode":
            proc = subprocess.run(
                ["opencode", "session", "list", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return None
            data = json.loads(proc.stdout)
            sessions = data if isinstance(data, list) else data.get("sessions", [])
            best = None
            best_delta = None
            for session in sessions:
                if not isinstance(session, dict):
                    continue
                ts = session.get("created_at") or session.get("started_at") or session.get("start")
                st = _parse_iso(ts) if isinstance(ts, str) else None
                if st is None or st < t0:
                    continue
                delta = st - t0
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best = session
            if not best:
                return None
            sid = best.get("id") or best.get("session_id")
            return str(sid) if sid is not None else None
    except Exception:
        return None
    return None


def _newest_jsonl_stem(root: Path, since: float, *, recursive: bool = False) -> str | None:
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
