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

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from gateway import actions_registry
from gateway.channels._http import http_json
from gateway.config import env_value
from gateway.format import to_markdown_v2

from .cards import Card, build_action_components_discord


LogFn = Callable[[str], None]


def _log_delivery_failure(instance_dir: Path, record: dict) -> None:
    """Append a structured failure entry to supervisor.jsonl.

    Edit failures are silent otherwise (the runner's ``log`` is a no-op in the
    daemon), so the actual Telegram/Slack/Discord error description is lost.
    Writing it here surfaces the root cause without changing the sender API.
    """
    log_path = instance_dir / "state" / "logs" / "supervisor.jsonl"
    record = {
        "kind": "supervisor_delivery_failure",
        "ts": datetime.now(timezone.utc).isoformat(),
        **record,
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        pass


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
    if card.reply_markup is not None:
        payload["reply_markup"] = json.dumps(card.reply_markup)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    mid = _post_or_fallback(url, payload, card.text, log=log)
    if mid is not None and card.short_token:
        actions_registry.attach_supervisor_message_by_token(
            card.short_token, mid, card_text=card.text
        )
    return mid


def delete_card_telegram(
    *,
    instance_dir: Path,
    chat_id: str,
    message_id: int,
    log: LogFn | None = None,
) -> bool:
    """Delete a card message. Returns True on success or if already gone."""
    token = env_value(instance_dir, "TELEGRAM_BOT_TOKEN")
    if not token or not chat_id or not message_id:
        return False
    payload: dict[str, Any] = {
        "chat_id": str(chat_id),
        "message_id": int(message_id),
    }
    url = f"https://api.telegram.org/bot{token}/deleteMessage"
    try:
        data = http_json(url, data=payload, timeout=15)
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"supervisor delete_card_telegram error: {exc}")
        return False
    if data.get("ok"):
        return True
    description = str(data.get("description") or "").lower()
    if "message to delete not found" in description or "message can't be deleted" in description:
        return True  # already gone — treat as success
    if log:
        log(f"supervisor delete_card_telegram failed: {data}")
    return False


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
    if card.reply_markup is not None:
        payload["reply_markup"] = json.dumps(card.reply_markup)
    if card.short_token:
        actions_registry.attach_supervisor_message_by_token(
            card.short_token, int(message_id), card_text=card.text
        )
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    try:
        data = http_json(url, data=payload, timeout=15)
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"supervisor edit_card_telegram error: {exc}")
        _log_delivery_failure(instance_dir, {
            "op": "edit_telegram", "chat_id": chat_id,
            "message_id": message_id, "exc": repr(exc),
        })
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
            data2 = http_json(url, data=fallback, timeout=15)
            if data2.get("ok"):
                return True
            _log_delivery_failure(instance_dir, {
                "op": "edit_telegram_plain", "chat_id": chat_id,
                "message_id": message_id,
                "first_error": data.get("description"),
                "fallback_error": data2.get("description"),
            })
            return False
        except Exception as exc:  # noqa: BLE001
            if log:
                log(f"supervisor edit_card_telegram fallback error: {exc}")
            _log_delivery_failure(instance_dir, {
                "op": "edit_telegram_plain", "chat_id": chat_id,
                "message_id": message_id,
                "first_error": data.get("description"),
                "fallback_exc": repr(exc),
            })
            return False
    if log:
        log(f"supervisor edit_card_telegram failed: {data}")
    _log_delivery_failure(instance_dir, {
        "op": "edit_telegram", "chat_id": chat_id,
        "message_id": message_id,
        "error_code": data.get("error_code"),
        "description": data.get("description"),
    })
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


def delete_card_slack(
    *,
    instance_dir: Path,
    channel: str,
    ts: str,
    log: LogFn | None = None,
) -> bool:
    """Delete card via chat.delete. Returns True on success or already gone."""
    token = env_value(instance_dir, "SLACK_BOT_TOKEN")
    if not token or not channel or not ts:
        return False
    payload: dict[str, Any] = {"channel": channel, "ts": ts}
    try:
        data = http_json("https://slack.com/api/chat.delete", token=token, data=payload, timeout=15)
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"supervisor delete_card_slack error: {exc}")
        return False
    if data.get("ok"):
        return True
    err = str(data.get("error") or "").lower()
    if "message_not_found" in err or "cant_delete_message" in err:
        return True
    if log:
        log(f"supervisor delete_card_slack failed: {data}")
    return False


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

# Discord embed colour for an in-flight supervisor card (blurple).
_DISCORD_CARD_COLOR = 0x5865F2


def _discord_card_payload(card: "Card") -> dict[str, Any]:
    """Build the Discord message body for a card.

    Plain cards (no action token) ship as ``content`` text — byte-identical
    to the pre-parity behavior, so existing delivery is untouched. Cards with
    actions enabled (``card.short_token`` set) ship as a richer **embed** plus
    an action row of Stop/Background **components**, the Discord twin of the
    Telegram inline keyboard.
    """
    if not card.short_token:
        return {"content": card.text[:2000]}
    return {
        "content": "",
        "embeds": [
            {
                "description": card.text[:4000],
                "color": _DISCORD_CARD_COLOR,
            }
        ],
        "components": build_action_components_discord(card.short_token),
    }


def send_card_discord(
    *,
    instance_dir: Path,
    channel_id: str,
    card: "Card",
    reply_to_message_id: str | None = None,
    log: LogFn | None = None,
) -> str | None:
    """Post card via Discord REST. Returns message_id as str, or None.

    When the card carries an action ``short_token`` (supervisor card actions
    enabled), the message renders as an embed with Stop/Background buttons and
    the token→message_id binding is recorded in the action registry so the
    Discord interaction handler can resolve a button click back to its session
    (the twin of ``send_card_telegram``'s ``attach_supervisor_message_by_token``).
    """
    token = env_value(instance_dir, "DISCORD_BOT_TOKEN")
    if not token or not channel_id:
        return None
    payload: dict[str, Any] = _discord_card_payload(card)
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
    if mid is None:
        return None
    if card.short_token:
        try:
            actions_registry.attach_supervisor_message_by_token(
                card.short_token, int(mid), card_text=card.text
            )
        except (TypeError, ValueError):
            pass
    return str(mid)


def delete_card_discord(
    *,
    instance_dir: Path,
    channel_id: str,
    message_id: str,
    log: LogFn | None = None,
) -> bool:
    """Delete card via Discord REST DELETE. Returns True on success or already gone."""
    token = env_value(instance_dir, "DISCORD_BOT_TOKEN")
    if not token or not channel_id or not message_id:
        return False
    url = f"{_DISCORD_API}/channels/{channel_id}/messages/{message_id}"
    try:
        http_json(
            url,
            extra_headers={"Authorization": f"Bot {token}"},
            method="DELETE",
            timeout=15,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        exc_str = str(exc).lower()
        if "404" in exc_str or "unknown message" in exc_str:
            return True
        if log:
            log(f"supervisor delete_card_discord error: {exc}")
        return False


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
    payload: dict[str, Any] = _discord_card_payload(card)
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
    if card.short_token:
        try:
            actions_registry.attach_supervisor_message_by_token(
                card.short_token, int(message_id), card_text=card.text
            )
        except (TypeError, ValueError):
            pass
    return "id" in data
