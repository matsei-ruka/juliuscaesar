"""Channel delivery for supervisor cards.

LOOP GUARD: This module MUST NOT write to ``state/transcripts/``. Cards are
ephemeral progress signals — including them in the conversation transcript
would make the brain's next turn think it said "scanning files" itself, and
recursive narration loops would follow. The supervisor uses raw channel APIs
(Telegram sendMessage / editMessageText) directly, bypassing
``gateway.delivery.deliver_response`` which writes to transcripts.

Telegram wired in Phase 2. Slack + Discord wired in Phase 4.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from gateway.channels._http import http_json
from gateway.config import env_value
from gateway.format import to_markdown_v2

from .cards import Card


LogFn = Callable[[str], None]


class DeliveryError(RuntimeError):
    """Raised when the channel API call fails after parse-mode fallback."""


def send_card_telegram(
    *,
    instance_dir: Path,
    chat_id: str,
    card: Card,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
    log: LogFn | None = None,
) -> int | None:
    """Post a new card. Returns the Telegram message_id, or None on failure.

    Sends with ``parse_mode=MarkdownV2`` and falls back to plain text on
    Telegram parse errors. Does NOT write to transcripts.
    """
    token = env_value(instance_dir, "TELEGRAM_BOT_TOKEN")
    if not token or not chat_id:
        return None
    payload: dict[str, Any] = {
        "chat_id": str(chat_id),
        "text": to_markdown_v2(card.text),
        "disable_web_page_preview": True,
        "disable_notification": True,
        "parse_mode": "MarkdownV2",
    }
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    return _post_or_fallback(url, payload, card.text, log=log)


def edit_card_telegram(
    *,
    instance_dir: Path,
    chat_id: str,
    message_id: int,
    card: Card,
    log: LogFn | None = None,
) -> bool:
    """Edit an existing card. Returns True on success.

    Telegram returns 400 if the new text matches the existing text exactly —
    that's treated as a successful no-op (returns True).
    """
    token = env_value(instance_dir, "TELEGRAM_BOT_TOKEN")
    if not token or not chat_id or not message_id:
        return False
    payload: dict[str, Any] = {
        "chat_id": str(chat_id),
        "message_id": int(message_id),
        "text": to_markdown_v2(card.text),
        "disable_web_page_preview": True,
        "parse_mode": "MarkdownV2",
    }
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    try:
        data = http_json(url, data=payload, timeout=15)
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"supervisor edit_card_telegram error: {exc}")
        return False

    if data.get("ok"):
        return True

    description = str(data.get("description") or "").lower()
    if "not modified" in description:
        return True
    if "parse" in description or "entit" in description:
        # Retry without parse_mode
        fallback = dict(payload)
        fallback.pop("parse_mode", None)
        fallback["text"] = card.text
        try:
            data = http_json(url, data=fallback, timeout=15)
            return bool(data.get("ok"))
        except Exception as exc:  # noqa: BLE001
            if log:
                log(f"supervisor edit_card_telegram fallback error: {exc}")
            return False
    if log:
        log(f"supervisor edit_card_telegram failed: {data}")
    return False


def _post_or_fallback(
    url: str,
    payload: dict[str, Any],
    original_text: str,
    *,
    log: LogFn | None,
) -> int | None:
    try:
        data = http_json(url, data=payload, timeout=15)
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"supervisor send_card_telegram error: {exc}")
        return None

    if not data.get("ok"):
        description = str(data.get("description") or "").lower()
        if "parse" in description or "entit" in description:
            fallback = dict(payload)
            fallback.pop("parse_mode", None)
            fallback["text"] = original_text
            try:
                data = http_json(url, data=fallback, timeout=15)
            except Exception as exc:  # noqa: BLE001
                if log:
                    log(f"supervisor send_card_telegram fallback error: {exc}")
                return None
        if not data.get("ok"):
            if log:
                log(f"supervisor send_card_telegram failed: {data}")
            return None

    result = data.get("result") or {}
    mid = result.get("message_id")
    try:
        return int(mid) if mid is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def send_card_slack(
    *,
    instance_dir: Path,
    channel: str,
    card: "Card",
    thread_ts: str | None = None,
    log: LogFn | None = None,
) -> str | None:
    """Post card via chat.postMessage. Returns ts (message id) or None."""
    token = env_value(instance_dir, "SLACK_BOT_TOKEN")
    if not token or not channel:
        return None
    payload: dict[str, Any] = {
        "channel": channel,
        "text": card.text,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        data = http_json("https://slack.com/api/chat.postMessage", token=token, data=payload, timeout=15)
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"supervisor send_card_slack error: {exc}")
        return None
    if not data.get("ok"):
        if log:
            log(f"supervisor send_card_slack failed: {data}")
        return None
    ts = data.get("ts")
    return str(ts) if ts is not None else None


def edit_card_slack(
    *,
    instance_dir: Path,
    channel: str,
    ts: str,
    card: "Card",
    log: LogFn | None = None,
) -> bool:
    """Edit card via chat.update. Returns True on success or no-op."""
    token = env_value(instance_dir, "SLACK_BOT_TOKEN")
    if not token or not channel or not ts:
        return False
    payload: dict[str, Any] = {
        "channel": channel,
        "ts": ts,
        "text": card.text,
    }
    try:
        data = http_json("https://slack.com/api/chat.update", token=token, data=payload, timeout=15)
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"supervisor edit_card_slack error: {exc}")
        return False
    if data.get("ok"):
        return True
    err = str(data.get("error") or "").lower()
    if "not_modified" in err:
        return True
    if "message_not_found" in err:
        # Message is gone (user deleted / channel archived). Tell caller so it
        # can clear ``channel_message_id`` and re-send instead of editing the
        # same dead ID forever (Bug #8).
        if log:
            log(f"supervisor edit_card_slack message_not_found: {data}")
        return False
    if log:
        log(f"supervisor edit_card_slack failed: {data}")
    return False


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

_DISCORD_API = "https://discord.com/api/v10"


def send_card_discord(
    *,
    instance_dir: Path,
    channel_id: str,
    card: "Card",
    reply_to_message_id: str | None = None,
    log: LogFn | None = None,
) -> str | None:
    """Post card via Discord REST. Returns message_id as str, or None."""
    token = env_value(instance_dir, "DISCORD_BOT_TOKEN")
    if not token or not channel_id:
        return None
    payload: dict[str, Any] = {"content": card.text[:2000]}
    if reply_to_message_id:
        payload["message_reference"] = {"message_id": str(reply_to_message_id)}
    url = f"{_DISCORD_API}/channels/{channel_id}/messages"
    try:
        data = http_json(
            url,
            extra_headers={"Authorization": f"Bot {token}"},
            data=payload,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"supervisor send_card_discord error: {exc}")
        return None
    mid = data.get("id")
    return str(mid) if mid is not None else None


def edit_card_discord(
    *,
    instance_dir: Path,
    channel_id: str,
    message_id: str,
    card: "Card",
    log: LogFn | None = None,
) -> bool:
    """Edit card via Discord REST PATCH. Returns True on success."""
    token = env_value(instance_dir, "DISCORD_BOT_TOKEN")
    if not token or not channel_id or not message_id:
        return False
    url = f"{_DISCORD_API}/channels/{channel_id}/messages/{message_id}"
    payload: dict[str, Any] = {"content": card.text[:2000]}
    try:
        data = http_json(
            url,
            extra_headers={"Authorization": f"Bot {token}"},
            data=payload,
            method="PATCH",
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"supervisor edit_card_discord error: {exc}")
        return False
    return "id" in data
