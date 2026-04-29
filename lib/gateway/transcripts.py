"""Per-conversation transcript log (append-only JSONL).

Layout: ``<instance>/state/transcripts/<conversation_id>.jsonl``. One event per
line. Inbound user messages and outbound assistant responses are appended by
the gateway at enqueue + delivery time. Files are append-only from the
agent's code path — readers (CLI, --resume context priming) never rewrite.

The format is JSON-per-line with a fixed key set:

    {"ts": "...Z", "role": "user|assistant", "text": "...",
     "message_id": "...", "channel": "telegram", "chat_id": "..."}

Unknown fields on read are tolerated; unknown fields on write are dropped.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator


@dataclass(frozen=True)
class TranscriptEvent:
    ts: str
    role: str
    text: str
    message_id: str | None = None
    channel: str | None = None
    chat_id: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def transcripts_dir(instance_dir: Path) -> Path:
    return instance_dir / "state" / "transcripts"


_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(conversation_id: str) -> str:
    """Coerce a conversation id into a safe filename component.

    Telegram chat ids are numeric (incl. negatives like ``-100...``) so this
    is mostly defensive against future channels with arbitrary identifiers.
    """
    cleaned = _SAFE_ID_RE.sub("_", conversation_id)
    return cleaned or "_unknown"


def transcript_path(instance_dir: Path, conversation_id: str) -> Path:
    return transcripts_dir(instance_dir) / f"{_safe_filename(conversation_id)}.jsonl"


def append(
    instance_dir: Path,
    *,
    conversation_id: str | None,
    role: str,
    text: str,
    message_id: str | None = None,
    channel: str | None = None,
    chat_id: str | None = None,
    ts: str | None = None,
) -> Path | None:
    """Append a single event to the transcript for ``conversation_id``.

    Returns the path written, or None when the write was skipped (no
    conversation_id, empty text). Best-effort: I/O errors are swallowed so a
    transcript failure cannot break message delivery.
    """
    if not conversation_id or not text:
        return None
    if role not in ("user", "assistant"):
        return None
    path = transcript_path(instance_dir, conversation_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        event = TranscriptEvent(
            ts=ts or _now_iso(),
            role=role,
            text=text,
            message_id=str(message_id) if message_id is not None else None,
            channel=channel,
            chat_id=chat_id,
        )
        line = json.dumps(asdict(event), ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return path
    except OSError:
        return None


def iter_events(path: Path) -> Iterator[TranscriptEvent]:
    """Yield events from a transcript file, skipping malformed lines."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            yield TranscriptEvent(
                ts=str(data.get("ts") or ""),
                role=str(data.get("role") or ""),
                text=str(data.get("text") or ""),
                message_id=(str(data["message_id"]) if data.get("message_id") is not None else None),
                channel=(str(data["channel"]) if data.get("channel") is not None else None),
                chat_id=(str(data["chat_id"]) if data.get("chat_id") is not None else None),
            )


def tail(path: Path, *, lines: int = 20) -> list[TranscriptEvent]:
    """Return the last N events from a transcript file."""
    if not path.exists() or lines <= 0:
        return []
    # For typical chat sizes this is fine; if files grow large, we read once
    # and slice. No need for reverse-scan optimization yet (out of scope).
    events = list(iter_events(path))
    return events[-lines:]


def list_conversations(instance_dir: Path) -> list[Path]:
    """List all transcript files under ``state/transcripts/``."""
    root = transcripts_dir(instance_dir)
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*.jsonl") if p.is_file())


def search(
    instance_dir: Path,
    query: str,
    *,
    since: str | None = None,
    role: str | None = None,
    limit: int = 50,
) -> list[tuple[Path, TranscriptEvent]]:
    """Substring search across all transcripts. Case-insensitive on ``query``.

    ``since`` filters by ``ts >= since`` (ISO-8601 string compare; UTC tz is
    consistent across writers). ``role`` restricts to user/assistant.
    """
    needle = query.lower()
    out: list[tuple[Path, TranscriptEvent]] = []
    for path in list_conversations(instance_dir):
        for ev in iter_events(path):
            if since and ev.ts < since:
                continue
            if role and ev.role != role:
                continue
            if needle and needle not in ev.text.lower():
                continue
            out.append((path, ev))
            if len(out) >= limit:
                return out
    return out


def get_by_message_id(
    instance_dir: Path,
    message_id: str,
) -> tuple[Path, TranscriptEvent] | None:
    """Find a transcript line by ``message_id``. Linear scan."""
    for path in list_conversations(instance_dir):
        for ev in iter_events(path):
            if ev.message_id == str(message_id):
                return path, ev
    return None


def render_priming_block(events: Iterable[TranscriptEvent]) -> str:
    """Format transcript events as a context-priming block for resume.

    Used by the brain preamble path to refresh context after a session is
    resumed from disk. Output is plain text — no markdown headers — so it
    plays nicely whatever the brain's preamble formatter does next.
    """
    lines: list[str] = []
    for ev in events:
        if not ev.text:
            continue
        speaker = "user" if ev.role == "user" else "assistant"
        ts = (ev.ts or "")[:19].replace("T", " ")
        prefix = f"[{ts}] {speaker}: " if ts else f"{speaker}: "
        # Single-line each line in the JSONL is already one logical message;
        # we keep newlines intact when present in the body so quotes survive.
        lines.append(prefix + ev.text)
    return "\n".join(lines)
