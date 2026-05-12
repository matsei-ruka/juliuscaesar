"""Action dispatchers for due commitments."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import Commitment


@dataclass(frozen=True)
class DispatchResult:
    ok: bool
    retryable: bool = True
    message: str = ""


def dispatch(instance_dir: Path, commitment: Commitment) -> DispatchResult:
    if commitment.action == "telegram-send":
        return telegram_send(instance_dir, commitment)
    if commitment.action == "jc-event":
        return jc_event(instance_dir, commitment)
    return DispatchResult(False, retryable=False, message=f"unknown action {commitment.action}")


def telegram_send(instance_dir: Path, commitment: Commitment) -> DispatchResult:
    from gateway.config import env_value  # type: ignore
    from heartbeat.lib.send_telegram import send  # type: ignore

    token = os.environ.get("TELEGRAM_BOT_TOKEN") or env_value(instance_dir, "TELEGRAM_BOT_TOKEN")
    chat_id = commitment.chat_id or env_value(instance_dir, "TELEGRAM_CHAT_ID")
    if not token:
        return DispatchResult(False, retryable=True, message="TELEGRAM_BOT_TOKEN not set")
    if not chat_id:
        return DispatchResult(False, retryable=False, message="chat_id not set")
    try:
        msg_id = send(commitment.text, token=str(token), chat_id=str(chat_id))
    except RuntimeError as exc:
        msg = str(exc)
        retryable = "HTTP 4" not in msg
        return DispatchResult(False, retryable=retryable, message=msg)
    return DispatchResult(True, retryable=False, message=f"telegram message_id={msg_id}")


def jc_event(instance_dir: Path, commitment: Commitment) -> DispatchResult:
    event_id = f"commitment-{commitment.slug}-{_utc_stamp()}"
    payload: dict[str, Any] = {
        "event_type": "commitment.due",
        "event_id": event_id,
        "slug": commitment.slug,
        "tags": list(commitment.tags),
        "action_metadata": dict(commitment.metadata),
        "notify_channel": "telegram",
    }
    if commitment.chat_id is not None:
        payload["notify_chat_id"] = str(commitment.chat_id)
    if commitment.text:
        payload["text"] = commitment.text
    path = instance_dir / "state" / "events" / f"{event_id}.json"
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        return DispatchResult(False, retryable=True, message=str(exc))
    return DispatchResult(True, retryable=False, message=str(path))


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
