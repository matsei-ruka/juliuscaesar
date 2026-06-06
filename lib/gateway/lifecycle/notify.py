"""§18.1 — operator notification on compaction.

Every successful compaction (from `/compact`, idle maintenance, or
`context_exhausted` recovery) emits a single Telegram message to the
instance's primary operator chat. Multiple slots compacted in the same
operation are batched into one message. Delivery is best-effort: a failed
send is logged as `context_compaction_notify_failed` and never reverses the
compaction.
"""

from __future__ import annotations

from dataclasses import dataclass

# Canonical trigger reasons surfaced in the notification header.
TRIGGER_COMPACT = "/compact"
TRIGGER_IDLE = "idle_maintenance"
TRIGGER_RECOVERY = "context_exhausted_recovery"


@dataclass(frozen=True)
class SlotCompaction:
    """One slot's before/after result inside a compaction operation."""

    brain: str
    slot: int
    tokens_before: int | None
    tokens_after: int | None
    method: str = "rotated"  # rotated | native_compaction


def _fmt_tokens(value: int | None) -> str:
    if value is None:
        return "?"
    if value >= 1000:
        return f"{round(value / 1000)}K"
    return str(value)


def build_notification_body(
    *,
    trigger: str,
    channel: str,
    conversation_id: str,
    slots: list[SlotCompaction],
    conversation_label: str | None = None,
) -> str:
    """Render the §18.1 message body.

    One header line, one location line, then one token line per compacted
    slot. The conversation hint prefers a human-readable label, else a
    truncated conversation id.
    """
    hint = conversation_label or _truncate_conv(conversation_id)
    lines = [f"🧹 Context compacted ({trigger})"]
    if len(slots) == 1:
        s = slots[0]
        lines.append(f"{channel} · {hint} · {s.brain} slot {s.slot}")
        lines.append(f"{_fmt_tokens(s.tokens_before)} → {_fmt_tokens(s.tokens_after)} tokens")
    else:
        lines.append(f"{channel} · {hint}")
        for s in slots:
            lines.append(
                f"{s.brain} slot {s.slot}: "
                f"{_fmt_tokens(s.tokens_before)} → {_fmt_tokens(s.tokens_after)} tokens"
            )
    return "\n".join(lines)


def _truncate_conv(conversation_id: str) -> str:
    cid = conversation_id or "-"
    return cid if len(cid) <= 16 else cid[:16] + "…"


def operator_chat_id(config) -> str | None:
    """First entry of `channels.telegram.chat_ids` — the operator's main chat."""
    telegram = config.channels.get("telegram") if config.channels else None
    if telegram is None:
        return None
    chat_ids = telegram.chat_ids or ()
    return str(chat_ids[0]) if chat_ids else None


def notify_compaction(
    runtime,
    *,
    trigger: str,
    channel: str,
    conversation_id: str,
    slots: list[SlotCompaction],
    conversation_label: str | None = None,
) -> bool:
    """Best-effort operator notification. Returns True iff a message was sent.

    Gated by `compaction_notify.enabled` (default true). Uses the existing
    Telegram channel send path — no rate-limiter or escaper bypass (§18.1).
    """
    config = runtime.config
    if not getattr(config, "compaction_notify", None) or not config.compaction_notify.enabled:
        return False
    if not slots:
        return False
    chat_id = operator_chat_id(config)
    if not chat_id:
        return False
    body = build_notification_body(
        trigger=trigger,
        channel=channel,
        conversation_id=conversation_id,
        slots=slots,
        conversation_label=conversation_label,
    )
    try:
        from ..channels.telegram import TelegramChannel
        from ..config import ChannelConfig

        cfg = config.channels.get("telegram") or ChannelConfig()
        tg = TelegramChannel(runtime.instance_dir, cfg, runtime.log)
        if not tg.ready():
            runtime.log(
                "context_compaction_notify_failed reason=telegram_not_ready",
                kind="context_compaction_notify_failed",
            )
            return False
        tg.send(body, {"chat_id": chat_id})
    except Exception as exc:  # noqa: BLE001
        runtime.log(
            f"context_compaction_notify_failed reason={exc!r}",
            kind="context_compaction_notify_failed",
        )
        return False
    runtime.log(
        f"context_compaction_notified trigger={trigger} channel={channel} "
        f"conv={_truncate_conv(conversation_id)} slots={len(slots)}",
        kind="context_compaction_notified",
    )
    return True
