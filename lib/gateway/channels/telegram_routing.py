"""Telegram inbound message routing helpers."""

from __future__ import annotations

from typing import Any, Callable


def should_process_message(
    message: dict[str, Any],
    *,
    bot_username: str | None,
    bot_user_id: int | None,
    get_chat_member_count: Callable[[str], int | None],
) -> bool:
    chat = message.get("chat") or {}
    chat_type = chat.get("type", "private")
    if chat_type in ("private", "channel"):
        return True
    if chat_type not in ("group", "supergroup"):
        return False
    reply_to = message.get("reply_to_message") or {}
    reply_from = reply_to.get("from") or {}
    if bot_user_id and reply_from.get("id") == bot_user_id:
        return True
    chat_id = str(chat.get("id", ""))
    if chat_id:
        member_count = get_chat_member_count(chat_id)
        if member_count is not None and member_count <= 2:
            return True
    if not bot_username:
        return False
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []
    for ent in entities:
        etype = ent.get("type")
        if etype == "mention":
            offset = ent.get("offset", 0)
            length = ent.get("length", 0)
            mentioned = text[offset : offset + length].lstrip("@").lower()
            if mentioned == bot_username:
                return True
        elif etype == "text_mention":
            user = ent.get("user") or {}
            if bot_user_id and user.get("id") == bot_user_id:
                return True
    return f"@{bot_username}" in text.lower()


def forward_ident(message: dict[str, Any]) -> str | None:
    ff = message.get("forward_from") or message.get("forward_from_chat")
    if not isinstance(ff, dict):
        return None
    return str(ff.get("username") or ff.get("title") or ff.get("id") or "-")
