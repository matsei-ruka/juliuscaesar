"""Session-expired handler — login-recovery Telegram round-trip.

When the classifier reports `session_expired` (auth invalid), we:

  1. Validate the operator chat is a private DM, not a group.
  2. Insert an `auth_pending(state=waiting)` row keyed on operator_chat. The
     unique partial index `auth_pending_one_active` enforces "at most one
     outstanding auth request per operator". A second concurrent failure
     appends its event_id to the existing row's pending_events list so all
     queued events get replayed on successful redemption.
  3. DM the operator with the login URL (validated against an allowlist of
     hosts) or a generic prompt to run `claude /login` in their screen.
  4. Defer the event — the pre-triage hook will consume the operator's token
     reply and call `redeem_token` (in `dispatcher.py`).
"""

from __future__ import annotations

import json

from .. import state as state_module
from .base import Defer, Fail, RecoveryContext, RecoveryDecision


_ALLOWED_LOGIN_HOSTS = ("claude.ai", "console.anthropic.com", "openrouter.ai")


def _validate_login_url(url: str | None) -> str | None:
    if not url:
        return None
    if not url.startswith("https://"):
        return None
    if not any(host in url for host in _ALLOWED_LOGIN_HOSTS):
        return None
    return url


class SessionExpiredHandler:
    GENERIC_PROMPT = (
        "🔐 Claude session expired. Run `claude /login` in your screen "
        "session and reply with the token here, or paste the auth token "
        "directly. Expires in 10 min."
    )

    def handle(self, event, classification, ctx: RecoveryContext) -> RecoveryDecision:
        operator_chat = self._operator_chat_from_event(event, ctx)
        if not operator_chat:
            return Fail(reason="auth_required_in_group")

        login_url = _validate_login_url(
            (classification.extracted or {}).get("login_url")
        )
        message = self._compose_message(login_url)

        from ... import queue

        conn = queue.connect(ctx.instance_dir)
        try:
            pending = state_module.insert_pending(
                conn,
                event_id=event.id,
                operator_chat=operator_chat,
                login_url=login_url or "",
            )
            if pending is None:
                # An outstanding row exists — append our event_id to its
                # replay queue so we get re-run on successful redemption.
                existing = state_module.get_active_pending(
                    conn, operator_chat=operator_chat
                )
                if existing is not None:
                    state_module.append_pending_event(
                        conn,
                        pending_id=existing.id,
                        event_id=event.id,
                    )
                    return Defer(
                        reason=f"auth_pending {existing.id} already waiting "
                        f"— appended event to pending_events"
                    )
                return Fail(reason="auth_pending insert failed and no existing row")
        finally:
            conn.close()

        self._send_operator_dm(ctx, operator_chat, message)
        return Defer(
            reason=f"auth_pending {pending.id} created — waiting for operator token"
        )

    def _operator_chat_from_event(self, event, ctx: RecoveryContext) -> str | None:
        """Resolve the operator's DM chat_id, or None if not DM-with-operator."""
        from ...config import env_value

        meta = _decode_meta(event)
        chat_type = meta.get("chat_type")
        if chat_type and chat_type != "private":
            return None
        operator = env_value(ctx.instance_dir, "TELEGRAM_CHAT_ID")
        meta_chat_id = meta.get("chat_id")
        if not operator:
            if chat_type == "private":
                return str(meta_chat_id or event.conversation_id or event.user_id or "")
            return None
        if meta_chat_id is not None:
            if str(meta_chat_id) != str(operator):
                return None
            if event.user_id and str(event.user_id) != str(operator):
                return None
            return str(meta_chat_id)
        if chat_type == "private":
            # Legacy queued Telegram events did not always include meta.chat_id.
            # A private DM can still target the configured operator chat.
            return str(operator)
        if event.user_id and str(event.user_id) == str(operator):
            return str(operator)
        return None

    def _compose_message(self, login_url: str | None) -> str:
        if login_url:
            return (
                f"🔐 Claude session expired. Visit:\n{login_url}\n\n"
                "then reply here with the auth token (expires in 10 min)."
            )
        return self.GENERIC_PROMPT

    def _send_operator_dm(self, ctx: RecoveryContext, chat_id: str, body: str) -> None:
        """Send a DM via TelegramChannel.send. Best-effort."""
        try:
            from ...channels.telegram import TelegramChannel
            from ...config import ChannelConfig

            cfg = ctx.config.channels.get("telegram") or ChannelConfig()
            channel = TelegramChannel(ctx.instance_dir, cfg, ctx.log)
            if not channel.ready():
                ctx.log("recovery session_expired: telegram channel not ready")
                return
            channel.send(body, {"chat_id": str(chat_id)})
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"recovery session_expired: DM failed: {exc}")


def _decode_meta(event) -> dict:
    if not event.meta:
        return {}
    try:
        data = json.loads(event.meta)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}
