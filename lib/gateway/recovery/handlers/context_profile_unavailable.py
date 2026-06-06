"""context_profile_unavailable handler — rotate onto a standard profile (§17.3).

Auth is fine and the context still fits a standard profile; only the requested
extended/1M/paid profile is unavailable. Clear the active slot mapping so the
next dispatch runs on the default (standard) profile and re-enqueue once. This
preserves context (rotation onto a fitting standard profile, not an exhaustion
drop) and never loops.
"""

from __future__ import annotations

import json

from ... import queue, router
from ...lifecycle import compaction
from .base import Fail, RecoveryContext, RecoveryDecision, Retry

_MARKER = "context_profile_recovery"


class ContextProfileUnavailableHandler:
    def handle(self, event, classification, ctx: RecoveryContext) -> RecoveryDecision:
        meta = _decode_meta(event)
        if meta.get(_MARKER):
            ctx.log(
                f"context_recovery_failed id={event.id} kind=context_profile_unavailable",
                event_id=getattr(event, "id", None),
                kind="context_recovery_failed",
            )
            return Fail(reason="context_profile_unavailable_recovery_failed")

        if not event.conversation_id:
            return Fail(reason="context_profile_unavailable without conversation_id")

        channel = router.channel_name(event)
        brain = _guess_brain(event, ctx, channel)
        if not brain:
            return Fail(reason="context_profile_unavailable without resolvable brain")

        rotated = _rotate_brain_slots(ctx, channel, event.conversation_id, brain)
        ctx.log(
            f"context_recovery id={event.id} kind=context_profile_unavailable brain={brain} "
            f"rotated={rotated}",
            event_id=getattr(event, "id", None),
            kind="context_recovery",
        )
        _reenqueue(event, ctx, meta)
        return Retry(
            reason="context_profile_unavailable — routed to standard profile",
            delay_seconds=0.0,
        )


def _rotate_brain_slots(ctx, channel, conversation_id, brain) -> int:
    conn = queue.connect(ctx.instance_dir)
    rotated = 0
    try:
        slots = compaction.list_conversation_slots(
            conn, channel=channel, conversation_id=conversation_id
        )
        for ref in slots:
            if ref.brain.split(":", 1)[0] != brain:
                continue
            if compaction.rotate_slot(
                conn,
                channel=channel,
                conversation_id=conversation_id,
                brain=ref.brain,
                slot=ref.slot,
                expected_session_id=ref.session_id,
            ) is not None:
                rotated += 1
    finally:
        conn.close()
    return rotated


def _reenqueue(event, ctx: RecoveryContext, meta: dict) -> None:
    meta = dict(meta)
    meta[_MARKER] = True
    new_msg_id = f"recovery:context_profile:{event.id}"
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
                error="recovery: context_profile_unavailable handoff — see redispatched event",
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
