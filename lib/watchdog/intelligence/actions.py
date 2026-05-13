"""Actions for intelligent watchdog decisions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from gateway import queue
from gateway.config import GatewayConfig, env_value
from gateway.delivery import deliver_response

from .models import Decision, EventSummary
from .snapshot import event_channel
from .state import IntelligenceState, now_iso


LogFn = Callable[[str], None]


def mark_event_notice(instance_dir: Path, summary: EventSummary, key: str) -> None:
    conn = queue.connect(instance_dir)
    try:
        current = queue.get(conn, summary.event.id)
        meta = _decode_meta(current) if current is not None else dict(summary.meta)
        raw_notices = meta.get("watchdog_notices")
        notices = dict(raw_notices) if isinstance(raw_notices, dict) else {}
        notices[key] = now_iso()
        meta["watchdog_notices"] = notices
        queue.update_meta(conn, summary.event.id, meta)
    finally:
        conn.close()


def notify_brain_issue(
    instance_dir: Path,
    gateway_config: GatewayConfig,
    summary: EventSummary,
    *,
    decision: Decision,
    log: LogFn,
) -> bool:
    preview = _preview_request(summary.event.content)
    if preview:
        body = (
            f"I could not get a clean answer for {preview} because the current "
            "brain hit a runtime/auth issue. I notified the operator."
        )
    else:
        body = (
            "The current brain hit a runtime/auth issue before it could answer. "
            "I notified the operator."
        )
    delivered = _deliver(instance_dir, gateway_config, summary, body, log=log)
    if decision.kind == "auth_expired":
        _notify_operator_auth(instance_dir, gateway_config, summary, log=log)
    return delivered


def mark_brain_unavailable(
    state: IntelligenceState,
    brain: str,
    *,
    reason: str,
    cooldown_seconds: int,
) -> None:
    until = datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
    state.mark_brain_unavailable(
        brain,
        reason=reason,
        until=until.isoformat(timespec="seconds").replace("+00:00", "Z"),
    )


def _deliver(
    instance_dir: Path,
    gateway_config: GatewayConfig,
    summary: EventSummary,
    body: str,
    *,
    log: LogFn,
) -> bool:
    meta = dict(summary.meta)
    channel = event_channel(summary)
    if channel not in gateway_config.channels or channel in ("cron", "jc-events"):
        operator = env_value(instance_dir, "TELEGRAM_CHAT_ID")
        if operator:
            channel = "telegram"
            meta["chat_id"] = str(operator)
    if channel == "telegram" and not meta.get("chat_id"):
        operator = env_value(instance_dir, "TELEGRAM_CHAT_ID")
        if operator:
            meta["chat_id"] = str(operator)
    meta.setdefault("delivery_channel", channel)
    message_id = deliver_response(
        instance_dir=instance_dir,
        source=channel,
        response=body,
        meta=meta,
        config_channels=gateway_config.channels,
        live_channels={},
        log=lambda msg, **fields: log(f"{msg} {fields}" if fields else msg),
    )
    return bool(message_id)


def _notify_operator_auth(
    instance_dir: Path,
    gateway_config: GatewayConfig,
    summary: EventSummary,
    *,
    log: LogFn,
) -> None:
    operator = _operator_chat(instance_dir, summary)
    if not operator:
        return
    body = _operator_auth_message(summary)
    meta: dict[str, Any] = {"delivery_channel": "telegram", "chat_id": str(operator)}
    deliver_response(
        instance_dir=instance_dir,
        source="telegram",
        response=body,
        meta=meta,
        config_channels=gateway_config.channels,
        live_channels={},
        log=lambda msg, **fields: log(f"{msg} {fields}" if fields else msg),
    )


def _operator_chat(instance_dir: Path, summary: EventSummary) -> str:
    operator = env_value(instance_dir, "TELEGRAM_CHAT_ID")
    if operator:
        return str(operator)
    if summary.meta.get("chat_type") == "private":
        chat = summary.meta.get("chat_id") or summary.event.conversation_id or summary.event.user_id
        return str(chat) if chat else ""
    return ""


def _operator_auth_message(summary: EventSummary) -> str:
    brain = summary.brain
    if brain in ("codex", "codex_api"):
        return (
            "Codex authentication appears expired for a pending gateway request. "
            "Run `jc codex-auth refresh` on the instance host."
        )
    return (
        "Claude authentication appears expired for a pending gateway request. "
        "Run `claude /login` on the instance host."
    )


def _decode_meta(event) -> dict[str, Any]:
    if event is None or not event.meta:
        return {}
    try:
        data = json.loads(event.meta)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _preview_request(content: str) -> str:
    text = " ".join((content or "").split())
    if not text:
        return ""
    if len(text) > 120:
        text = text[:117].rstrip() + "..."
    return f'"{text}"'
