"""Silence detection from gateway transcripts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gateway import transcripts

from .conf import TrackedChat


@dataclass(frozen=True)
class SilenceState:
    chat: TrackedChat
    last_user_ts: datetime | None
    silence_hours: float | None
    transcript_path: Path

    @property
    def has_inbound(self) -> bool:
        return self.last_user_ts is not None


def detect(instance_dir: Path, chat: TrackedChat, *, now: datetime | None = None) -> SilenceState:
    current = _as_utc(now or datetime.now(timezone.utc))
    path = transcripts.transcript_path(instance_dir, str(chat.chat_id))
    last = None
    for event in transcripts.iter_events(path):
        if event.role != "user" or not event.ts:
            continue
        parsed = _parse_ts(event.ts)
        if parsed is not None:
            last = parsed
    if last is None:
        return SilenceState(chat=chat, last_user_ts=None, silence_hours=None, transcript_path=path)
    delta = current - _as_utc(last)
    return SilenceState(
        chat=chat,
        last_user_ts=last,
        silence_hours=max(0.0, delta.total_seconds() / 3600.0),
        transcript_path=path,
    )


def _parse_ts(value: str) -> datetime | None:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
