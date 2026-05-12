"""Schema and YAML I/O for deferred commitments."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - install requires pyyaml.
    yaml = None  # type: ignore


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
VALID_ACTIONS = {"telegram-send", "jc-event"}
VALID_REPEATS = {None, "daily", "weekly"}
VALID_ORIGINS = {"agent", "reengage", "heartbeat", "manual", "dream"}


class CommitmentError(ValueError):
    """Raised when a commitment is malformed."""


@dataclass(frozen=True)
class Commitment:
    slug: str
    created_at: datetime
    due_at: datetime
    action: str
    text: str = ""
    chat_id: int | None = None
    tags: tuple[str, ...] = ()
    repeat: str | None = None
    origin: str = "manual"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def retries(self) -> int:
        raw = self.metadata.get("retries", 0)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    def with_metadata(self, **updates: Any) -> "Commitment":
        metadata = dict(self.metadata)
        metadata.update(updates)
        return Commitment(
            slug=self.slug,
            created_at=self.created_at,
            due_at=self.due_at,
            action=self.action,
            text=self.text,
            chat_id=self.chat_id,
            tags=self.tags,
            repeat=self.repeat,
            origin=self.origin,
            metadata=metadata,
        )

    def with_due_at(self, due_at: datetime) -> "Commitment":
        return Commitment(
            slug=self.slug,
            created_at=self.created_at,
            due_at=due_at,
            action=self.action,
            text=self.text,
            chat_id=self.chat_id,
            tags=self.tags,
            repeat=self.repeat,
            origin=self.origin,
            metadata=self.metadata,
        )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CommitmentError(f"{field_name} must be a non-empty ISO-8601 string")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CommitmentError(f"{field_name} is not valid ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CommitmentError(f"{field_name} must include an explicit timezone offset")
    return parsed


def format_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat(timespec="seconds")


def _coerce_tags(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if not isinstance(value, list):
        raise CommitmentError("tags must be a list or comma-separated string")
    out: list[str] = []
    for item in value:
        tag = str(item).strip()
        if tag:
            out.append(tag)
    return tuple(out)


def _coerce_chat_id(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CommitmentError("chat_id must be an integer") from exc


def from_dict(data: dict[str, Any], *, source_path: Path | None = None) -> Commitment:
    slug = str(data.get("slug") or "").strip()
    if not SLUG_RE.match(slug):
        raise CommitmentError("slug must match ^[a-z0-9][a-z0-9-]{0,63}$")
    if source_path is not None and source_path.parent.name not in {"done", "failed"}:
        if source_path.stem != slug:
            raise CommitmentError(f"slug {slug!r} must match filename {source_path.name!r}")

    action = str(data.get("action") or "").strip()
    if action not in VALID_ACTIONS:
        raise CommitmentError(f"action must be one of {sorted(VALID_ACTIONS)}")

    repeat_raw = data.get("repeat")
    repeat = None if repeat_raw in (None, "", "null") else str(repeat_raw).strip()
    if repeat not in VALID_REPEATS:
        raise CommitmentError("repeat must be null, daily, or weekly")

    origin = str(data.get("origin") or "manual").strip()
    if origin not in VALID_ORIGINS:
        raise CommitmentError(f"origin must be one of {sorted(VALID_ORIGINS)}")

    text = str(data.get("text") or "")
    if action == "telegram-send" and not text.strip():
        raise CommitmentError("text is required for telegram-send")
    if len(text) > 4000:
        raise CommitmentError("text must be <= 4000 characters")

    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise CommitmentError("metadata must be a mapping")

    return Commitment(
        slug=slug,
        created_at=parse_datetime(data.get("created_at"), field_name="created_at"),
        due_at=parse_datetime(data.get("due_at"), field_name="due_at"),
        action=action,
        text=text,
        chat_id=_coerce_chat_id(data.get("chat_id")),
        tags=_coerce_tags(data.get("tags")),
        repeat=repeat,
        origin=origin,
        metadata=dict(metadata),
    )


def to_dict(commitment: Commitment) -> dict[str, Any]:
    return {
        "slug": commitment.slug,
        "created_at": format_datetime(commitment.created_at),
        "due_at": format_datetime(commitment.due_at),
        "action": commitment.action,
        "chat_id": commitment.chat_id,
        "text": commitment.text,
        "tags": list(commitment.tags),
        "repeat": commitment.repeat,
        "origin": commitment.origin,
        "metadata": dict(commitment.metadata),
    }


def load(path: Path) -> Commitment:
    if yaml is None:
        raise ImportError("PyYAML required to load commitments")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise CommitmentError(f"{path}: commitment YAML must be a mapping")
    return from_dict(data, source_path=path)


def dump(commitment: Commitment, path: Path) -> None:
    if yaml is None:
        raise ImportError("PyYAML required to write commitments")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(to_dict(commitment), sort_keys=False, default_flow_style=False)
    path.write_text(text, encoding="utf-8")
