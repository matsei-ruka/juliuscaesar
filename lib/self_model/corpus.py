"""Read agent self-observation corpus from transcripts and HOT.md.

Unlike user_model.corpus (which reads gateway queue.db), self_model reads transcripts
directly — the agent observes its OWN assistant messages, not user messages.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class Event:
    """Simplified self-observation event."""
    id: str
    conversation_id: str
    role: str  # assistant | user
    text: str
    ts: str


@dataclass(frozen=True)
class HotObservation:
    """A `#self-observation`-tagged block from HOT.md."""
    heading: str
    body: str


def iter_assistant_messages(
    instance_dir: Path,
    look_back_days: int = 7,
) -> Iterator[Event]:
    """Iterate assistant messages from state/transcripts/*.jsonl within window."""
    transcripts_dir = instance_dir / "state" / "transcripts"
    if not transcripts_dir.exists():
        return

    cutoff = (datetime.now(timezone.utc) - timedelta(days=look_back_days)).isoformat()

    for path in sorted(transcripts_dir.glob("*.jsonl")):
        conversation_id = path.stem
        try:
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("role") != "assistant":
                    continue
                ts = data.get("ts") or ""
                if ts and ts < cutoff:
                    continue
                yield Event(
                    id=f"{conversation_id}:{i}",
                    conversation_id=conversation_id,
                    role="assistant",
                    text=data.get("text", ""),
                    ts=ts,
                )
        except OSError:
            continue


def iter_user_messages(
    instance_dir: Path,
    look_back_days: int = 7,
) -> Iterator[Event]:
    """Iterate user messages from transcripts (used by filippo_correction detector)."""
    transcripts_dir = instance_dir / "state" / "transcripts"
    if not transcripts_dir.exists():
        return

    cutoff = (datetime.now(timezone.utc) - timedelta(days=look_back_days)).isoformat()

    for path in sorted(transcripts_dir.glob("*.jsonl")):
        conversation_id = path.stem
        try:
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("role") != "user":
                    continue
                ts = data.get("ts") or ""
                if ts and ts < cutoff:
                    continue
                yield Event(
                    id=f"{conversation_id}:{i}",
                    conversation_id=conversation_id,
                    role="user",
                    text=data.get("text", ""),
                    ts=ts,
                )
        except OSError:
            continue


def iter_hot_observations(instance_dir: Path) -> Iterator[HotObservation]:
    """Yield H2 blocks in HOT.md tagged with `#self-observation`."""
    hot_path = instance_dir / "memory" / "L1" / "HOT.md"
    if not hot_path.exists():
        return

    content = hot_path.read_text(encoding="utf-8")
    # Split on H2 headings, keep heading line.
    blocks = re.split(r"(?m)^(?=## )", content)
    for block in blocks:
        if "#self-observation" not in block:
            continue
        lines = block.splitlines()
        if not lines:
            continue
        heading = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        yield HotObservation(heading=heading, body=body)


def count_assistant_messages(instance_dir: Path, look_back_days: int = 7) -> int:
    """Count assistant messages in window."""
    return sum(1 for _ in iter_assistant_messages(instance_dir, look_back_days))
