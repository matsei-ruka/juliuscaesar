"""Reflection phase: gather instance deltas for a dream window."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gateway import transcripts

from .schema import CommitmentRecord, Reflection, SentRecord, TranscriptDelta


def reflect(
    instance_dir: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Reflection:
    end = _as_utc(until or datetime.now(timezone.utc))
    start = _as_utc(since or _last_dream_end(instance_dir) or (end - timedelta(days=1)))
    return Reflection(
        window_start=start,
        window_end=end,
        transcript_deltas=_transcript_deltas(instance_dir, start, end),
        memory_state_hash=_memory_state_hash(instance_dir),
        sent_deltas=_sent_deltas(instance_dir, start, end),
        closed_commitments=_closed_commitments(instance_dir, start, end),
    )


def _transcript_deltas(instance_dir: Path, start: datetime, end: datetime) -> list[TranscriptDelta]:
    out: list[TranscriptDelta] = []
    for path in transcripts.list_conversations(instance_dir):
        events = []
        for ev in transcripts.iter_events(path):
            if not ev.ts:
                continue
            parsed = _try_parse_ts(ev.ts)
            if parsed is not None and start <= parsed <= end:
                events.append(ev)
        if not events:
            continue
        out.append(
            TranscriptDelta(
                conversation_id=path.stem,
                path=str(path.relative_to(instance_dir)),
                events=len(events),
                first_ts=events[0].ts,
                last_ts=events[-1].ts,
            )
        )
    return out


def _memory_state_hash(instance_dir: Path) -> str:
    h = hashlib.sha256()
    root = instance_dir / "memory"
    for path in sorted(root.rglob("*.md")) if root.exists() else []:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        h.update(str(path.relative_to(instance_dir)).encode("utf-8"))
        h.update(_frontmatter(text).encode("utf-8"))
    return "sha256:" + h.hexdigest()[:16]


def _sent_deltas(instance_dir: Path, start: datetime, end: datetime) -> list[SentRecord]:
    path = instance_dir / "heartbeat" / "state" / "sent.log"
    if not path.exists():
        return []
    records: list[SentRecord] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = str(data.get("ts") or "")
        if ts and start <= _parse_ts(ts) <= end:
            records.append(SentRecord(ts=ts, body_preview=str(data.get("body_preview") or "")))
    return records


def _closed_commitments(instance_dir: Path, start: datetime, end: datetime) -> list[CommitmentRecord]:
    root = instance_dir / "state" / "commitments" / "done"
    if not root.exists():
        return []
    out: list[CommitmentRecord] = []
    for path in sorted(root.glob("*.yaml")):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if start <= mtime <= end:
            slug = path.name.split(".")[0]
            out.append(CommitmentRecord(slug=slug, path=str(path.relative_to(instance_dir)), ts=mtime.isoformat()))
    return out


def _last_dream_end(instance_dir: Path) -> datetime | None:
    root = instance_dir / "state" / "dreams"
    if not root.exists():
        return None
    reports = sorted(root.glob("*.md"))
    for path in reversed(reports):
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("window_end: "):
                return _parse_ts(line.partition(": ")[2].strip().strip('"'))
    return None


def _frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---", 4)
    return text[: end + 4] if end != -1 else ""


def _parse_ts(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    return _as_utc(parsed)


def _try_parse_ts(value: str) -> datetime | None:
    try:
        return _parse_ts(value)
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
