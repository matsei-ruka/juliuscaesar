"""Goal cache — the brain-agnostic task anchor store.

A goal is the objective of the task a conversation is currently executing.
It is stored on disk, keyed by ``conversation_id`` (PR #64 sets that to
``task-root:<root_id>`` on task events), and read on every dispatch so the
brain can be re-anchored without re-explaining the task each turn.

Design (see docs/specs/goal-integration.md):

- **Single-writer**: ``set``/``clear``/``sweep`` are called only from the
  gateway dispatch loop, so there is one writer and no lock is needed.
- **Atomic**: writes go through a tempfile + ``os.replace``.
- **Multi-reader**: ``get``/``goal_text`` are read-only and safe to call from
  slot worker threads — they always see a coherent old-or-new file.
- **TTL age-fallback**: ``get`` treats an entry older than ``ttl_seconds`` as
  absent (§5.3 B) so a goal cannot leak forever even if the
  ``company.task_closed`` clear signal never arrives. Expired entries are not
  written away by readers; ``sweep`` (dispatch loop) prunes them.

The brain never holds goal state (brains are rebuilt per dispatch); this
module is the source of truth.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GOAL_TEXT_MAX_CHARS = 500
GOAL_TEXT_MAX_LINES = 20
DEFAULT_TTL_SECONDS = 3600  # 1h floor; the precise clear is company.task_closed

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _path(instance_dir: Path) -> Path:
    return Path(instance_dir) / "state" / "gateway" / "goal.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        base = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(base)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _load(instance_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads(_path(instance_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(instance_dir: Path, data: dict[str, Any]) -> bool:
    """Atomic whole-file write. Single-writer (dispatch loop). Returns success."""
    path = _path(instance_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _is_expired(entry: dict[str, Any], ttl_seconds: int | None) -> bool:
    if not ttl_seconds or ttl_seconds <= 0:
        return False
    set_at = _parse_iso(str(entry.get("set_at") or ""))
    if set_at is None:
        return False  # no timestamp → never expire on age alone
    return (_now() - set_at).total_seconds() > ttl_seconds


# --- writers (dispatch loop only) ------------------------------------------


def set(instance_dir: Path, conversation_id: str, task_id: str, text: str) -> bool:
    """Anchor ``conversation_id`` on ``task_id``/``text``. Idempotent per
    (conversation_id, task_id) — re-setting the same task refreshes ``set_at``.
    """
    if not conversation_id:
        return False
    data = _load(instance_dir)
    data[conversation_id] = {
        "task_id": str(task_id),
        "text": text,
        "set_at": _now_iso(),
    }
    return _save(instance_dir, data)


def clear(instance_dir: Path, conversation_id: str, task_id: str | None = None) -> bool:
    """Drop the goal for ``conversation_id``. If ``task_id`` is given, clear
    only when it matches the stored one (defensive against stale/out-of-order
    close events for a conversation already re-anchored on a new task)."""
    if not conversation_id:
        return False
    data = _load(instance_dir)
    entry = data.get(conversation_id)
    if not entry:
        return False
    if task_id is not None and str(entry.get("task_id")) != str(task_id):
        return False  # stale clear — preserve the active goal
    del data[conversation_id]
    return _save(instance_dir, data)


def sweep(instance_dir: Path, ttl_seconds: int | None = DEFAULT_TTL_SECONDS) -> int:
    """Prune expired entries. Dispatch-loop only. Returns count removed."""
    if not ttl_seconds or ttl_seconds <= 0:
        return 0
    data = _load(instance_dir)
    expired = [k for k, v in data.items() if isinstance(v, dict) and _is_expired(v, ttl_seconds)]
    if not expired:
        return 0
    for k in expired:
        del data[k]
    _save(instance_dir, data)
    return len(expired)


# --- readers (any thread) --------------------------------------------------


def get(
    instance_dir: Path,
    conversation_id: str,
    *,
    ttl_seconds: int | None = DEFAULT_TTL_SECONDS,
) -> dict[str, Any] | None:
    """Return the goal entry for ``conversation_id``, or ``None`` if absent or
    age-expired. Read-only (never writes — see ``sweep`` for pruning)."""
    if not conversation_id:
        return None
    entry = _load(instance_dir).get(conversation_id)
    if not isinstance(entry, dict):
        return None
    if _is_expired(entry, ttl_seconds):
        return None
    return entry


def goal_text(
    instance_dir: Path,
    conversation_id: str,
    *,
    ttl_seconds: int | None = DEFAULT_TTL_SECONDS,
) -> str:
    """Goal text for ``conversation_id``, or "" if none. The value brains
    inject (system prompt or prompt body)."""
    entry = get(instance_dir, conversation_id, ttl_seconds=ttl_seconds)
    return str(entry.get("text") or "") if entry else ""


def all_goals(instance_dir: Path) -> dict[str, Any]:
    """All current entries (for jc-doctor / observability). Not TTL-filtered."""
    return _load(instance_dir)


# --- goal text formatting --------------------------------------------------


def _sanitise(text: str) -> str:
    text = _CONTROL_CHARS.sub("", text or "")
    lines = text.splitlines()
    if len(lines) > GOAL_TEXT_MAX_LINES:
        lines = lines[:GOAL_TEXT_MAX_LINES]
    text = "\n".join(lines).strip()
    if len(text) > GOAL_TEXT_MAX_CHARS:
        text = text[: GOAL_TEXT_MAX_CHARS - 1].rstrip() + "…"
    return text


def format_goal(meta: dict[str, Any]) -> str:
    """Build the goal text from a ``company.task_assigned`` event's ``meta``.

    Prefers explicit ``title``/``description`` (added by the company-inbox
    channel); falls back to a payload title if present. Capped + sanitised.
    """
    if not isinstance(meta, dict):
        return ""
    title = str(meta.get("title") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not title and not description:
        payload = meta.get("payload")
        if isinstance(payload, dict):
            title = str(payload.get("title") or "").strip()
            description = str(payload.get("description") or "").strip()
    body = "\n\n".join(p for p in (title, description) if p)
    return _sanitise(body)
