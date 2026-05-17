"""WhatsApp channel local state helpers.

Keeps chat records, pending senders, draft responses, and event log
behind a small API so CLIs and runtime code do not duplicate filesystem rules.

Matches the email channel's ``email_state.py`` pattern.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


WHATSAPP_STATE_REL = Path("state/channels/whatsapp")
CHATS_FILE = "chats.jsonl"
PENDING_SENDERS_FILE = "pending_senders.json"
DRAFTS_DIR = "drafts"
EVENTS_FILE = "events.jsonl"


def _state_root(instance_dir: Path) -> Path:
    return Path(instance_dir) / WHATSAPP_STATE_REL


def chats_path(instance_dir: Path) -> Path:
    return _state_root(instance_dir) / CHATS_FILE


def drafts_dir(instance_dir: Path) -> Path:
    return _state_root(instance_dir) / DRAFTS_DIR


def events_path(instance_dir: Path) -> Path:
    return _state_root(instance_dir) / EVENTS_FILE


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Chats ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChatRecord:
    jid: str
    push_name: str = ""
    chat_type: str = "dm"       # dm | group
    tier: str = "external"      # trusted | external | blocked
    last_message_at: str = ""
    account_id: str = "default"


def read_chats(instance_dir: Path) -> list[ChatRecord]:
    """Read all known WhatsApp chats from chats.jsonl."""
    path = chats_path(instance_dir)
    if not path.exists():
        return []
    chats: list[ChatRecord] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            chats.append(ChatRecord(
                jid=str(data.get("jid", "")),
                push_name=str(data.get("push_name", "")),
                chat_type=str(data.get("chat_type", "dm")),
                tier=str(data.get("tier", "external")),
                last_message_at=str(data.get("last_message_at", "")),
                account_id=str(data.get("account_id", "default")),
            ))
    except OSError:
        return []
    return chats


def upsert_chat(instance_dir: Path, chat: ChatRecord) -> None:
    """Add or update a chat record. Deduplicates by jid + account_id."""
    chats = read_chats(instance_dir)
    # Remove existing
    chats = [
        c for c in chats
        if not (c.jid == chat.jid and c.account_id == chat.account_id)
    ]
    chats.append(chat)
    path = chats_path(instance_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({
            "jid": c.jid,
            "push_name": c.push_name,
            "chat_type": c.chat_type,
            "tier": c.tier,
            "last_message_at": c.last_message_at,
            "account_id": c.account_id,
        }, sort_keys=True, default=str)
        for c in chats
    ]
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ── Drafts ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DraftRecord:
    draft_id: str
    state: str = "pending"      # pending | approved | denied | sent
    response: str = ""
    meta: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.meta is None:
            object.__setattr__(self, "meta", {})


def write_draft(instance_dir: Path, draft_id: str, response: str, meta: dict[str, Any]) -> DraftRecord:
    """Persist a draft response for an External sender."""
    ddir = drafts_dir(instance_dir)
    ddir.mkdir(parents=True, exist_ok=True)
    record = DraftRecord(
        draft_id=draft_id,
        state="pending",
        response=response,
        meta=meta,
    )
    tmp = ddir / f".{draft_id}.json.tmp"
    tmp.write_text(json.dumps({
        "draft_id": draft_id,
        "state": "pending",
        "response": response,
        "meta": meta,
    }, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, ddir / f"{draft_id}.json")
    return record


def read_draft(instance_dir: Path, draft_id: str) -> DraftRecord | None:
    """Read a single draft by id."""
    path = drafts_dir(instance_dir) / f"{draft_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return DraftRecord(
        draft_id=str(data.get("draft_id", draft_id)),
        state=str(data.get("state", "pending")),
        response=str(data.get("response", "")),
        meta=data.get("meta") if isinstance(data.get("meta"), dict) else {},
    )


def update_draft_state(instance_dir: Path, draft_id: str, state: str) -> DraftRecord | None:
    """Transition a draft to a new state (approved, denied, sent)."""
    draft = read_draft(instance_dir, draft_id)
    if draft is None:
        return None
    path = drafts_dir(instance_dir) / f"{draft_id}.json"
    data = {
        "draft_id": draft.draft_id,
        "state": state,
        "response": draft.response,
        "meta": draft.meta,
    }
    tmp = path.with_name(f".{draft_id}.json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)
    return DraftRecord(draft_id=draft_id, state=state, response=draft.response, meta=draft.meta)


def remove_draft(instance_dir: Path, draft_id: str) -> None:
    path = drafts_dir(instance_dir) / f"{draft_id}.json"
    path.unlink(missing_ok=True)


def pending_drafts(instance_dir: Path) -> list[DraftRecord]:
    """Return all drafts still in 'pending' state."""
    ddir = drafts_dir(instance_dir)
    if not ddir.is_dir():
        return []
    drafts: list[DraftRecord] = []
    for path in sorted(ddir.glob("*.json")):
        draft = read_draft(instance_dir, path.stem)
        if draft and draft.state == "pending":
            drafts.append(draft)
    return drafts


# ── Events ──────────────────────────────────────────────────────────────────

def record_event(instance_dir: Path, **fields: Any) -> None:
    """Append a structured event to events.jsonl."""
    path = events_path(instance_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"ts": now_ts(), **fields}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, default=str, sort_keys=True) + "\n")


def recent_events(instance_dir: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent events from events.jsonl."""
    path = events_path(instance_dir)
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
