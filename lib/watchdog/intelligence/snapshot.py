"""Collect bounded gateway state for intelligent watchdog evaluation."""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gateway import queue
from gateway.router import channel_name

from .config import IntelligenceConfig
from .models import EventSummary, LogEntry, Snapshot


KEYWORDS = (
    "dispatch begin",
    "dispatch failed",
    "adapter timeout",
    "recovery classify",
    "recovery fail",
    "recovery defer",
    "triage error",
    "event failed",
    "authentication",
    "auth",
    "login",
    "session",
    "401",
    "timeout",
)


def build_snapshot(
    instance_dir: Path,
    cfg: IntelligenceConfig,
    *,
    now: datetime | None = None,
) -> Snapshot:
    now = now or datetime.now(timezone.utc)
    logs = read_gateway_logs(instance_dir, max_lines=cfg.log_window_lines)
    brain_by_event = _brain_map(logs)
    running = _read_running(instance_dir, cfg, brain_by_event=brain_by_event, now=now)
    event_ids = {item.event.id for item in running}
    relevant_logs = [
        entry
        for entry in logs
        if (entry.event_id in event_ids if entry.event_id is not None else False)
        or any(keyword in (entry.msg or entry.raw).lower() for keyword in KEYWORDS)
    ][-cfg.log_window_lines :]
    return Snapshot(running=running, logs=relevant_logs)


def read_gateway_logs(instance_dir: Path, *, max_lines: int) -> list[LogEntry]:
    path = queue.queue_dir(instance_dir) / "gateway.log"
    if not path.exists():
        return []
    lines: deque[str] = deque(maxlen=max_lines)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.strip():
                    lines.append(line.rstrip("\n"))
    except OSError:
        return []
    return [_parse_log_line(line) for line in lines]


def _read_running(
    instance_dir: Path,
    cfg: IntelligenceConfig,
    *,
    brain_by_event: dict[int, tuple[str, str | None]],
    now: datetime,
) -> list[EventSummary]:
    conn = queue.connect(instance_dir)
    try:
        rows = conn.execute(
            """
            SELECT * FROM events
            WHERE status='running'
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()
    out: list[EventSummary] = []
    for row in rows:
        event = queue.row_to_event(row)
        if event is None:
            continue
        age = _event_age(event, now=now)
        if age < cfg.long_running_notice_seconds:
            continue
        out.append(_summary(event, age=age, brain_by_event=brain_by_event))
    return out


def _summary(
    event: queue.Event,
    *,
    age: float,
    brain_by_event: dict[int, tuple[str, str | None]],
) -> EventSummary:
    meta = _decode_meta(event)
    brain, model = brain_by_event.get(event.id, ("", None))
    if not brain:
        spec = meta.get("brain_override") or meta.get("brain") or ""
        if isinstance(spec, str) and spec.strip():
            brain, _, model_part = spec.partition(":")
            model = model_part or None
    if not brain:
        brain = "unknown"
    return EventSummary(
        event=event,
        meta=meta,
        age_seconds=age,
        brain=brain,
        model=model,
        status=event.status,
        error=event.error or "",
    )


def _parse_log_line(line: str) -> LogEntry:
    if line.startswith("{"):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return LogEntry(raw=line, msg=line)
        if isinstance(data, dict):
            event_id = _int_or_none(data.get("event_id"))
            return LogEntry(
                raw=line,
                ts=str(data.get("ts") or ""),
                msg=str(data.get("msg") or ""),
                kind=str(data.get("kind") or ""),
                event_id=event_id,
                brain=str(data.get("brain") or ""),
                model=str(data.get("model") or ""),
            )
    return LogEntry(raw=line, msg=line)


def _brain_map(logs: list[LogEntry]) -> dict[int, tuple[str, str | None]]:
    out: dict[int, tuple[str, str | None]] = {}
    for entry in logs:
        if entry.event_id is None:
            event_id = _event_id_from_msg(entry.msg)
        else:
            event_id = entry.event_id
        if event_id is None:
            continue
        brain = entry.brain or _value_after(entry.msg, "brain=")
        model = entry.model or _value_after(entry.msg, "model=")
        if brain:
            out[event_id] = (brain, None if model in ("", "-") else model)
    return out


def _event_age(event: queue.Event, *, now: datetime) -> float:
    ts = event.started_at or event.received_at
    parsed = _parse_ts(ts)
    if parsed is None:
        return 0.0
    return max(0.0, now.timestamp() - parsed.timestamp())


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _decode_meta(event: queue.Event) -> dict[str, Any]:
    if not event.meta:
        return {}
    try:
        data = json.loads(event.meta)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_id_from_msg(msg: str) -> int | None:
    for marker in ("id=", "event="):
        value = _value_after(msg, marker)
        if value:
            return _int_or_none(value)
    return None


def _value_after(msg: str, marker: str) -> str:
    idx = msg.find(marker)
    if idx < 0:
        return ""
    rest = msg[idx + len(marker) :]
    return rest.split()[0].strip(",;")


def event_channel(summary: EventSummary) -> str:
    return str(summary.meta.get("delivery_channel") or channel_name(summary.event))
