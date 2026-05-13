"""Actions for intelligent watchdog decisions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from gateway import brain_spec, capabilities, queue
from gateway.brains.dispatch import _BRAIN_REGISTRY
from gateway.config import BrainOverrideConfig, GatewayConfig, env_value
from gateway.delivery import deliver_response
from gateway.recovery import state as recovery_state

from .config import IntelligenceConfig
from .models import Decision, EventSummary
from .snapshot import event_channel
from .state import IntelligenceState, now_iso


LogFn = Callable[[str], None]


def notify_long_running(
    instance_dir: Path,
    gateway_config: GatewayConfig,
    summary: EventSummary,
    decision: Decision,
    *,
    log: LogFn,
) -> bool:
    body = decision.notice.strip() if decision.source == "triage_model" else ""
    if not body:
        preview = _preview_request(summary.event.content)
        if preview:
            body = f"I am still working on: {preview}"
        else:
            body = "I am still working on this and will keep the request open."
    return _deliver(instance_dir, gateway_config, summary, body, log=log)


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
    fallback: str | None,
    decision: Decision,
    log: LogFn,
) -> bool:
    preview = _preview_request(summary.event.content)
    if fallback:
        if preview:
            body = (
                f"I could not get a clean answer for {preview} because the current "
                f"brain hit a runtime/auth issue. I am retrying it with {fallback} now."
            )
        else:
            body = (
                "The current brain hit a runtime/auth issue before it could answer. "
                f"I am retrying it with {fallback} now."
            )
    else:
        if preview:
            body = (
                f"I could not get a clean answer for {preview} because the current "
                "brain hit a runtime/auth issue. I notified the operator and will retry "
                "when the session is healthy again."
            )
        else:
            body = (
                "The current brain hit a runtime/auth issue before it could answer. "
                "I notified the operator and will retry when the session is healthy again."
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


def select_fallback_brain(
    instance_dir: Path,
    gateway_config: GatewayConfig,
    intelligence_config: IntelligenceConfig,
    state: IntelligenceState,
    summary: EventSummary,
) -> str | None:
    if not intelligence_config.brain_switch_enabled:
        return None
    current = summary.brain
    candidates = list(intelligence_config.brain_fallbacks.get(current, ()))
    if gateway_config.triage.fallback_brain:
        candidates.append(gateway_config.triage.fallback_brain)
    for spec in candidates:
        try:
            parsed = brain_spec.parse(str(spec))
        except ValueError:
            continue
        if parsed.brain == current:
            continue
        if state.is_brain_unavailable(parsed.brain):
            continue
        if summary.meta.get("image_path") and not capabilities.supports_images(parsed.brain):
            continue
        if not _brain_validates(instance_dir, gateway_config, parsed.brain):
            continue
        return parsed.format()
    return None


def switch_event_to_brain(
    instance_dir: Path,
    summary: EventSummary,
    *,
    target_brain: str,
    decision: Decision,
) -> None:
    meta = dict(summary.meta)
    if meta.get("watchdog_switch"):
        return
    meta["brain_override"] = target_brain
    meta["watchdog_switch"] = {
        "from": summary.brain_spec,
        "to": target_brain,
        "reason": decision.kind,
        "at": now_iso(),
    }
    conn = queue.connect(instance_dir)
    try:
        queue.update_meta(conn, summary.event.id, meta)
        queue.retry_now(conn, summary.event.id)
    finally:
        conn.close()


def ensure_auth_pending(instance_dir: Path, summary: EventSummary) -> bool:
    operator = _operator_chat(instance_dir, summary)
    if not operator:
        return False
    conn = queue.connect(instance_dir)
    try:
        pending = recovery_state.insert_pending(
            conn,
            event_id=summary.event.id,
            operator_chat=operator,
            login_url="",
        )
        if pending is None:
            existing = recovery_state.get_active_pending(conn, operator_chat=operator)
            if existing is not None:
                recovery_state.append_pending_event(
                    conn,
                    pending_id=existing.id,
                    event_id=summary.event.id,
                )
        return True
    finally:
        conn.close()


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
            "Run `jc codex-auth refresh` on the instance host, then retry or let the queued event replay."
        )
    return (
        "Claude authentication appears expired for a pending gateway request. "
        "Run `claude /login` on the instance host, then retry or let the queued event replay."
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


def _brain_validates(instance_dir: Path, gateway_config: GatewayConfig, brain: str) -> bool:
    cls = _BRAIN_REGISTRY.get(brain)
    if cls is None:
        return False
    override = gateway_config.brains.get(brain) or BrainOverrideConfig()
    try:
        if brain == "codex_api":
            instance = cls(instance_dir, override=override, codex_auth_cfg=gateway_config.codex_auth)
        else:
            instance = cls(instance_dir, override=override)
        instance.validate()
    except Exception:
        return False
    return True
