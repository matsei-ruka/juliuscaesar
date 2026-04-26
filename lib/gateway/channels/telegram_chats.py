"""Telegram chat-directory recording helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .. import chats as chats_module
from .base import LogFn


def dm_title(chat: dict[str, Any]) -> str | None:
    """Compose a DM display name as `first_name + last_name` per spec."""
    first = (chat.get("first_name") or "").strip()
    last = (chat.get("last_name") or "").strip()
    full = " ".join(part for part in (first, last) if part)
    if full:
        return full
    username = chat.get("username")
    if username:
        return f"@{username}"
    return None


def cached_member_count(
    member_count_cache: dict[str, tuple[int, float]],
    chat_id: str,
) -> int | None:
    """Return a cached `getChatMemberCount` value if present."""
    entry = member_count_cache.get(chat_id)
    return entry[0] if entry else None


def record_chat(
    *,
    instance_dir: Path,
    conn_factory: Callable[[], object],
    member_count_cache: dict[str, tuple[int, float]],
    log: LogFn,
    chat: dict[str, Any],
    message: dict[str, Any],
) -> None:
    """Upsert the inbound chat into the chats directory."""
    try:
        chat_id = str(chat.get("id", ""))
        if not chat_id:
            return
        chat_type = chat.get("type")
        if chat_type in ("group", "supergroup", "channel"):
            title = chat.get("title") or dm_title(chat)
        else:
            title = dm_title(chat) or chat.get("title")
        chats_module.upsert_chat(
            instance_dir=instance_dir,
            conn=conn_factory(),
            channel="telegram",
            chat_id=chat_id,
            chat_type=chat_type,
            title=title,
            username=chat.get("username"),
            member_count=cached_member_count(member_count_cache, chat_id),
            last_message_id=(
                str(message.get("message_id"))
                if message.get("message_id") is not None
                else None
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log(f"telegram chat upsert failed chat_id={chat.get('id')}: {exc}")
