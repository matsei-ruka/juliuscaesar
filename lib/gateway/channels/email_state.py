"""Email channel local state helpers.

Keeps pending inbound messages, outbound drafts, UID watermarks, and draft
lifecycle state behind one small API so CLIs and runtime code do not duplicate
filesystem rules.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PENDING_REL = Path("state/channels/email/pending")
DRAFTS_REL = Path("state/channels/email/drafts")
LAST_UID_REL = Path("state/channels/email/last_uid")
EVENTS_REL = Path("state/channels/email/events.jsonl")


@dataclass(frozen=True)
class StateRecord:
    path: Path
    data: dict[str, Any]


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pending_dir(instance_dir: Path) -> Path:
    return Path(instance_dir) / PENDING_REL


def drafts_dir(instance_dir: Path) -> Path:
    return Path(instance_dir) / DRAFTS_REL


def last_uid_file(instance_dir: Path) -> Path:
    return Path(instance_dir) / LAST_UID_REL


def events_file(instance_dir: Path) -> Path:
    return Path(instance_dir) / EVENTS_REL


def read_last_uid(instance_dir: Path) -> int:
    path = last_uid_file(instance_dir)
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except (FileNotFoundError, ValueError):
        return 0


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _iter_json(root: Path) -> Iterable[StateRecord]:
    if not root.is_dir():
        return []
    records: list[StateRecord] = []
    for path in sorted(root.glob("*/*.json")):
        data = _read_json(path)
        if data is not None:
            records.append(StateRecord(path=path, data=data))
    return records


def pending_records(instance_dir: Path) -> list[StateRecord]:
    return list(_iter_json(pending_dir(instance_dir)))


def draft_records(instance_dir: Path, *, states: set[str] | None = None) -> list[StateRecord]:
    records = list(_iter_json(drafts_dir(instance_dir)))
    if states is None:
        return records
    return [record for record in records if str(record.data.get("state", "")) in states]


def find_draft(instance_dir: Path, draft_id: str) -> StateRecord | None:
    for record in draft_records(instance_dir):
        if record.data.get("draft_id") == draft_id:
            return record
    return None


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def record_event(instance_dir: Path, event: str, **fields: Any) -> None:
    data = {"ts": now_ts(), "event": event, **fields}
    path = events_file(instance_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, default=str, sort_keys=True) + "\n")


def recent_events(instance_dir: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    path = events_file(instance_dir)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            events.append(data)
    return events


def update_draft(record: StateRecord, **fields: Any) -> dict[str, Any]:
    data = dict(record.data)
    data.update(fields)
    write_json_atomic(record.path, data)
    return data


def remove_record(record: StateRecord) -> None:
    record.path.unlink(missing_ok=True)
    try:
        record.path.parent.rmdir()
    except OSError:
        pass


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _oldest_age_seconds(values: Iterable[Any]) -> int | None:
    now = datetime.now(timezone.utc)
    oldest: datetime | None = None
    for value in values:
        dt = _parse_ts(value)
        if dt is None:
            continue
        if oldest is None or dt < oldest:
            oldest = dt
    if oldest is None:
        return None
    return max(0, int((now - oldest).total_seconds()))


def metrics(instance_dir: Path) -> dict[str, Any]:
    pending = pending_records(instance_dir)
    drafts = draft_records(instance_dir)
    recent = recent_events(instance_dir, limit=200)
    draft_states: dict[str, int] = {}
    for record in drafts:
        state = str(record.data.get("state") or "unknown")
        draft_states[state] = draft_states.get(state, 0) + 1
    event_counts: dict[str, int] = {}
    for event in recent:
        name = str(event.get("event") or "unknown")
        event_counts[name] = event_counts.get(name, 0) + 1
    return {
        "last_uid": read_last_uid(instance_dir),
        "pending": len(pending),
        "drafts": len(drafts),
        "draft_states": draft_states,
        "event_counts_recent": event_counts,
        "last_event": recent[-1] if recent else None,
        "oldest_pending_age_seconds": _oldest_age_seconds(
            (r.data.get("metadata") or {}).get("date") for r in pending
        ),
        "oldest_draft_age_seconds": _oldest_age_seconds(
            r.data.get("draft_timestamp") for r in drafts if r.data.get("state") == "pending"
        ),
    }
