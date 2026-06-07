"""context_exhausted handler — checkpoint-free rotation + one redispatch (§17.3).

The resumed native session grew past the model's usable context. Do not
generic-retry against the poisoned session: rotate the active slot(s) for the
brain (clear the resume mapping, slot-scoped + race-safe) and re-enqueue the
same event once with `context_rotation_recovery=true`. The next dispatch runs
without `--resume`, the brain opens a fresh session seeded by L1 + transcript.

Rotation is a compaction event, so the operator notification fires once
(trigger `context_exhausted_recovery`, §18.1).
"""

from __future__ import annotations

import json

from ... import queue, router
from ...lifecycle import compaction, notify
from .base import Fail, RecoveryContext, RecoveryDecision, Retry

_MARKER = "context_rotation_recovery"


class ContextExhaustedHandler:
    def handle(self, event, classification, ctx: RecoveryContext) -> RecoveryDecision:
        meta = _decode_meta(event)
        if meta.get(_MARKER):
            # Already rotated once and the fresh session failed the same way —
            # never loop. Surface visibly (§17.3).
            ctx.log(
                f"context_recovery_failed id={event.id} kind=context_exhausted — "
                "fresh session also exhausted",
                event_id=getattr(event, "id", None),
                kind="context_recovery_failed",
            )
            return Fail(reason="context_exhausted_recovery_failed")

        if not event.conversation_id:
            return Fail(reason="context_exhausted without conversation_id")

        channel = router.channel_name(event)
        brain = _guess_brain(event, ctx, channel)
        if not brain:
            return Fail(reason="context_exhausted without resolvable brain")

        compacted = _rotate_brain_slots(ctx, channel, event.conversation_id, brain)
        if compacted:
            _maybe_notify(ctx, channel, event.conversation_id, compacted)
        ctx.log(
            f"context_recovery id={event.id} kind=context_exhausted brain={brain} "
            f"rotated={len(compacted)}",
            event_id=getattr(event, "id", None),
            kind="context_recovery",
        )
        _reenqueue(event, ctx, meta)
        return Retry(reason="context_exhausted — rotated, redispatched fresh", delay_seconds=0.0)


def _rotate_brain_slots(ctx, channel, conversation_id, brain):
    conn = queue.connect(ctx.instance_dir)
    compacted = []
    try:
        slots = compaction.list_conversation_slots(
            conn, channel=channel, conversation_id=conversation_id
        )
        for ref in slots:
            if ref.brain.split(":", 1)[0] != brain:
                continue
            result = compaction.rotate_slot(
                conn,
                channel=channel,
                conversation_id=conversation_id,
                brain=ref.brain,
                slot=ref.slot,
                expected_session_id=ref.session_id,
            )
            if result is not None:
                compacted.append(result)
    finally:
        conn.close()
    return compacted


def _maybe_notify(ctx, channel, conversation_id, compacted) -> None:
    runtime = getattr(ctx, "runtime", None)
    if runtime is None or not hasattr(runtime, "config"):
        return
    try:
        notify.notify_compaction(
            runtime,
            trigger=notify.TRIGGER_RECOVERY,
            channel=channel,
            conversation_id=conversation_id,
            slots=compacted,
        )
    except Exception as exc:  # noqa: BLE001
        ctx.log(f"context_compaction_notify_failed reason={exc!r}", kind="context_compaction_notify_failed")


def _reenqueue(event, ctx: RecoveryContext, meta: dict) -> None:
    meta = dict(meta)
    meta[_MARKER] = True
    new_msg_id = f"recovery:context_exhausted:{event.id}"
    conn = queue.connect(ctx.instance_dir)
    try:
        queue.enqueue(
            conn,
            source=event.source,
            source_message_id=new_msg_id,
            user_id=event.user_id,
            conversation_id=event.conversation_id,
            content=event.content,
            meta=meta,
            available_at=queue.now_iso(),
        )
        try:
            queue.fail(
                conn,
                event.id,
                error="recovery: context_exhausted handoff — see redispatched event",
                max_retries=0,
            )
        except KeyError:
            pass
    finally:
        conn.close()


def _guess_brain(event, ctx: RecoveryContext, channel: str) -> str | None:
    if not event.conversation_id:
        return None
    conn = queue.connect(ctx.instance_dir)
    try:
        row = conn.execute(
            "SELECT brain FROM sessions WHERE channel=? AND conversation_id=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (channel, event.conversation_id),
        ).fetchone()
    finally:
        conn.close()
    if row:
        return str(row["brain"]).split(":", 1)[0]
    default, _ = ctx.config.brain_for(channel)
    return default


def _decode_meta(event) -> dict:
    if not event.meta:
        return {}
    try:
        data = json.loads(event.meta)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}
