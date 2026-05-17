"""Build per-event snapshots for the supervisor tick."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gateway import queue

from .config import SupervisorConfig
from .models import AdapterInfo, EventSnapshot
from .phases import classify


def build_snapshots(
    instance_dir: Path,
    cfg: SupervisorConfig,
    *,
    now: datetime | None = None,
) -> list[EventSnapshot]:
    now = now or datetime.now(timezone.utc)
    log_lines = _read_gateway_log_tail(instance_dir)
    brain_map = _brain_map_from_log(log_lines)
    pid_map = _pid_map_from_log(log_lines)
    worker_convs = _linked_worker_conversations(instance_dir)
    return _read_qualifying(
        instance_dir,
        cfg,
        now=now,
        brain_map=brain_map,
        pid_map=pid_map,
        worker_convs=worker_convs,
    )


def _read_qualifying(
    instance_dir: Path,
    cfg: SupervisorConfig,
    *,
    now: datetime,
    brain_map: dict[int, tuple[str, str | None]],
    pid_map: dict[int, int],
    worker_convs: set[str],
) -> list[EventSnapshot]:
    conn = queue.connect(instance_dir)
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE status='running' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    out: list[EventSnapshot] = []
    for row in rows:
        event = queue.row_to_event(row)
        if event is None:
            continue
        age = _event_age(event, now=now)
        brain, model = brain_map.get(event.id, ("unknown", None))
        threshold = cfg.notice_threshold(brain)
        if age < threshold:
            continue
        meta = _decode_meta(event)
        worker_linked = (event.conversation_id or "") in worker_convs
        adapter = _build_adapter_info(
            instance_dir, event.id, pid_map.get(event.id), cfg.stderr_tail_bytes
        )
        language = _detect_language(meta, event.content)
        phase = classify(
            adapter.stderr_tail,
            mtime_age_seconds=adapter.activity_age_seconds,
            has_stderr=bool(adapter.stderr_tail),
        )
        out.append(
            EventSnapshot(
                event=event,
                meta=meta,
                age_seconds=age,
                brain=brain,
                model=model,
                adapter=adapter,
                phase=phase,
                worker_linked=worker_linked,
                language=language,
            )
        )
    return out


def _build_adapter_info(
    instance_dir: Path,
    event_id: int,
    pid: int | None,
    tail_bytes: int,
) -> AdapterInfo:
    stderr_dir = instance_dir / "state" / "gateway" / "adapter_stderr"
    stderr_path: Path | None = None
    stderr_mtime: float | None = None
    stderr_tail = ""

    if stderr_dir.is_dir():
        candidates = sorted(
            stderr_dir.glob(f"{event_id}-*"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
        )
        if candidates:
            stderr_path = candidates[-1]
            try:
                stat = stderr_path.stat()
                stderr_mtime = stat.st_mtime
                stderr_tail = _read_tail(stderr_path, tail_bytes)
            except OSError:
                pass

    pid_alive = _is_pid_alive(pid) if pid is not None else False

    return AdapterInfo(
        event_id=event_id,
        pid=pid,
        stderr_path=stderr_path,
        stderr_mtime=stderr_mtime,
        stderr_tail=stderr_tail,
        pid_alive=pid_alive,
    )


def _read_tail(path: Path, nbytes: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > nbytes:
                fh.seek(size - nbytes)
            return fh.read(nbytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _is_pid_alive(pid: int) -> bool:
    """Probe ``pid`` with signal 0.

    ``PermissionError`` means the PID exists but belongs to a different user —
    that is still *alive*. Returning False on permission error would let the
    supervisor falsely declare a foreign-user (or recycled) PID dead and reset
    the event out from under a healthy adapter (Bug #5).
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_gateway_log_tail(instance_dir: Path, max_lines: int = 200) -> list[str]:
    path = queue.queue_dir(instance_dir) / "gateway.log"
    if not path.exists():
        return []
    lines: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.rstrip("\n")
                if stripped.strip():
                    lines.append(stripped)
    except OSError:
        return []
    return lines[-max_lines:]


def _brain_map_from_log(lines: list[str]) -> dict[int, tuple[str, str | None]]:
    out: dict[int, tuple[str, str | None]] = {}
    for line in lines:
        event_id = _extract_int(line, "event=")
        if event_id is None:
            continue
        brain = _extract_token(line, "brain=")
        model = _extract_token(line, "model=")
        if brain:
            out[event_id] = (brain, model or None)
    return out


def _pid_map_from_log(lines: list[str]) -> dict[int, int]:
    out: dict[int, int] = {}
    for line in lines:
        if "adapter spawn" not in line:
            continue
        event_id = _extract_int(line, "event=")
        pid = _extract_int(line, "pid=")
        if event_id is not None and pid is not None:
            out[event_id] = pid
    return out


def _linked_worker_conversations(instance_dir: Path) -> set[str]:
    path = instance_dir / "state" / "workers" / "index.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(data, list):
        return set()
    out: set[str] = set()
    for entry in data:
        if isinstance(entry, dict) and entry.get("status") == "running":
            conv = entry.get("conversation_id")
            if conv:
                out.add(str(conv))
    return out


def _event_age(event: queue.Event, *, now: datetime) -> float:
    ts = event.started_at or event.received_at
    if not ts:
        return 0.0
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return max(0.0, now.timestamp() - parsed.timestamp())
    except ValueError:
        return 0.0


def _decode_meta(event: queue.Event) -> dict[str, Any]:
    if not event.meta:
        return {}
    try:
        data = json.loads(event.meta)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _detect_language(meta: dict[str, Any], content: str) -> str:
    lang = meta.get("language")
    if lang and isinstance(lang, str):
        return lang[:2].lower()
    # Heuristic: common Italian function words
    text = (content or "").lower()
    it_markers = (
        " di ", " che ", " per ", " non ", " una ",
        " con ", " in ", " il ", " la ", "ciao", "grazie",
    )
    if sum(1 for m in it_markers if m in text) >= 2:
        return "it"
    return "en"


def _extract_int(line: str, marker: str) -> int | None:
    idx = line.find(marker)
    if idx < 0:
        return None
    rest = line[idx + len(marker):]
    # Stop at first whitespace, comma, or semicolon
    token = _first_token(rest)
    try:
        return int(token)
    except (ValueError, TypeError):
        return None


def _extract_token(line: str, marker: str) -> str:
    idx = line.find(marker)
    if idx < 0:
        return ""
    rest = line[idx + len(marker):]
    return _first_token(rest)


def _first_token(s: str) -> str:
    """Return everything up to the first whitespace, comma, or semicolon."""
    if not s:
        return ""
    end = len(s)
    for i, ch in enumerate(s):
        if ch in " \t\n\r,;":
            end = i
            break
    return s[:end]
