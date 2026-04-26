"""Session-missing handler — silent sticky-clear + redispatch.

Triggered when `--resume <uuid>` is rejected because the session does not
exist (file deleted, account switched, fresh install). Clears the sticky
session-id mapping and re-enqueues the same event with a
`session_missing_redispatch=True` meta flag. The next dispatch runs without
`--resume`, claude generates a new session-id, the gateway records it as the
new sticky.
"""

from __future__ import annotations

import json
import re

from ... import queue, router
from ..session_drop import clear_sticky_session
from .base import Defer, Fail, RecoveryContext, RecoveryDecision


_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


class SessionMissingHandler:
    def handle(self, event, classification, ctx: RecoveryContext) -> RecoveryDecision:
        meta = _decode_meta(event)
        if meta.get("session_missing_redispatch"):
            # We already redispatched this once. If it failed again, sticky-
            # clear didn't take or the brain itself is broken — surface as a
            # hard fail with a distinct reason so logs/alerts can flag it.
            return Fail(reason="session_missing_recovery_failed")

        session_id = (
            (classification.extracted or {}).get("session_id")
            or _extract_uuid(classification.raw)
        )
        if not event.conversation_id:
            return Fail(reason="session_missing without conversation_id")

        channel = router.channel_name(event)
        brain_guess = self._guess_brain(event, ctx, channel)
        if not brain_guess:
            return Fail(reason="session_missing without resolvable brain")

        cleared = clear_sticky_session(
            ctx.instance_dir,
            channel=channel,
            conversation_id=event.conversation_id,
            brain=brain_guess,
            session_id=session_id,
        )
        if not cleared:
            # Concurrent handler already cleared sticky and (presumably)
            # redispatched. Re-enqueue ours behind it to avoid a duplicate.
            self._reenqueue(event, ctx, defer_seconds=2.0, racing=True)
            return Defer(
                reason="session_missing_racing — re-enqueued behind concurrent handler"
            )

        self._reenqueue(event, ctx, defer_seconds=0.0, racing=False)
        return Defer(
            reason="session_missing — sticky cleared, redispatched without --resume"
        )

    def _reenqueue(
        self,
        event,
        ctx: RecoveryContext,
        *,
        defer_seconds: float,
        racing: bool,
    ) -> None:
        meta = _decode_meta(event)
        meta["session_missing_redispatch"] = True
        if racing:
            meta["session_missing_racing"] = True
        new_msg_id = f"recovery:session_missing:{event.id}"
        conn = queue.connect(ctx.instance_dir)
        try:
            available_at = queue.now_iso()
            if defer_seconds > 0:
                available_at = queue.add_seconds(available_at, int(defer_seconds))
            queue.enqueue(
                conn,
                source=event.source,
                source_message_id=new_msg_id,
                user_id=event.user_id,
                conversation_id=event.conversation_id,
                content=event.content,
                meta=meta,
                available_at=available_at,
            )
            # Mark the original as a recovery handoff so the dispatcher's
            # caller does not also fail/retry it. The original may not exist
            # in tests where the handler is exercised in isolation.
            try:
                queue.fail(
                    conn,
                    event.id,
                    error="recovery: session_missing handoff — see redispatched event",
                    max_retries=0,
                )
            except KeyError:
                pass
        finally:
            conn.close()

    def _guess_brain(self, event, ctx: RecoveryContext, channel: str) -> str | None:
        """Recover the brain that was used at dispatch time.

        We don't carry that in the failure payload, so read the existing
        sessions row for `(channel, conversation_id)` (the one we're about
        to clear). Falls back to the per-channel default brain.
        """
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
            spec = str(row["brain"])
            return spec.split(":", 1)[0]
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


def _extract_uuid(text: str) -> str | None:
    if not text:
        return None
    m = _UUID_RE.search(text)
    return m.group(0) if m else None
