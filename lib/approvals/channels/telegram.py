"""Telegram notify + decide adapter for unified approvals."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..models import Approval
from ..principal import load_principal


logger = logging.getLogger("approvals.channels.telegram")


CALLBACK_PREFIX = "apv:"


def notify(instance_dir: Path, record: Approval) -> bool:
    """Send the approval card to the principal's DM. Returns True on success."""
    if not record.notify_telegram:
        return False
    principal = load_principal(instance_dir)
    if not principal.telegram_chat_id:
        logger.info(
            "approvals telegram notify skipped: no main chat (kind=%s)", record.kind
        )
        return False

    from gateway.config import env_value

    token = env_value(instance_dir, "TELEGRAM_BOT_TOKEN")
    if not token:
        logger.info("approvals telegram notify skipped: no bot token")
        return False

    body = render_card(record)
    keyboard = inline_keyboard(record)
    payload: dict[str, Any] = {
        "chat_id": principal.telegram_chat_id,
        "text": body,
        "disable_web_page_preview": True,
        "reply_markup": json.dumps(keyboard),
    }
    try:
        from gateway.channels._http import http_json

        data = http_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("approvals telegram sendMessage failed: %s", exc)
        return False

    if not data.get("ok"):
        logger.warning("approvals telegram sendMessage rejected: %s", data)
        return False

    result = data.get("result") or {}
    message_id = result.get("message_id")
    chat_id_resp = (result.get("chat") or {}).get("id")
    from ..service import mark_notified

    mark_notified(
        instance_dir,
        record.approval_id,
        extra_callback_payload={
            "tg_message_id": message_id,
            "tg_chat_id": chat_id_resp,
        },
    )
    return True


def render_card(record: Approval) -> str:
    """Render plain-text card body (Telegram default parse_mode, no MarkdownV2 risk)."""
    title = record.title or record.kind
    body = (record.body or "").strip()
    if len(body) > 800:
        body = body[:800].rstrip() + f"\n…\n(use `jc approvals show {record.short_id}`)"
    lines = [
        f"🟡 Approval pending — {record.kind}",
        "",
        title,
    ]
    if body:
        lines.extend(["", body])
    expires_blurb = ""
    if record.expires_at:
        expires_blurb = f" · expires {record.expires_at}"
    lines.extend(["", f"id: {record.short_id}{expires_blurb}"])
    return "\n".join(lines)


def inline_keyboard(record: Approval) -> dict[str, Any]:
    """Two-button inline keyboard with `apv:<id>:<approve|reject>` callback_data."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Approve",
                    "callback_data": f"{CALLBACK_PREFIX}{record.approval_id}:approve",
                },
                {
                    "text": "❌ Reject",
                    "callback_data": f"{CALLBACK_PREFIX}{record.approval_id}:reject",
                },
            ]
        ]
    }


def parse_callback_data(data: str) -> tuple[str, str] | None:
    """Return `(approval_id, action)` for an `apv:` callback or None."""
    if not data or not data.startswith(CALLBACK_PREFIX):
        return None
    payload = data[len(CALLBACK_PREFIX) :]
    parts = payload.split(":", 1)
    if len(parts) != 2:
        return None
    approval_id, action = parts
    if action not in ("approve", "reject"):
        return None
    return approval_id, action


def handle_callback_query(
    instance_dir: Path,
    *,
    callback_data: str,
    from_user_id: str,
) -> dict[str, Any]:
    """Validate principal + decide. Returns a dict for the channel router to use."""
    parsed = parse_callback_data(callback_data)
    if parsed is None:
        return {"ok": False, "error": "bad_payload"}
    approval_id, action = parsed

    principal = load_principal(instance_dir)
    if principal.telegram_user_id and str(from_user_id) != str(principal.telegram_user_id):
        logger.info(
            "approvals telegram decide rejected: from=%s principal=%s",
            from_user_id,
            principal.telegram_user_id,
        )
        return {"ok": False, "error": "not_authorized"}

    from ..models import ApprovalNotFound, ApprovalConflict
    from ..service import decide

    try:
        record = decide(
            instance_dir,
            approval_id,
            action=action,
            decided_by=f"tg:{from_user_id}",
            decision_channel="telegram",
        )
    except ApprovalNotFound:
        return {"ok": False, "error": "not_found", "approval_id": approval_id}
    except ApprovalConflict as exc:
        return {"ok": False, "error": "conflict", "detail": str(exc)}
    return {"ok": True, "approval": record, "action": action}
