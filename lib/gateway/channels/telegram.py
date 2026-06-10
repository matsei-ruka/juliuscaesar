"""Telegram long-poll channel."""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .. import chats as chats_module
from .. import queue as queue_module
from ..config import ChannelConfig, env_value, load_config_cached
from ..config_writer import (
    env_chat_ids as _env_chat_ids,
    update_env_chat_ids,
    update_gateway_yaml_chat_lists,
)
from ..format import to_markdown_v2
from ._http import http_json
from .base import EnqueueFn, LogFn
from .telegram_chats import (
    cached_member_count as cached_telegram_member_count,
    dm_title as telegram_dm_title,
    record_chat as record_telegram_chat,
)
from .telegram_media import (
    AUDIO_MIME_EXT as _AUDIO_MIME_EXT,
    VOICE_MIME_EXT as _VOICE_MIME_EXT,
    download_telegram_file as _download_telegram_file,
    ingest_audio_attachment,
    ingest_document,
    ingest_photo,
    transcribe_audio as _transcribe_audio,
)
from .telegram_outbound import (
    encode_multipart as _encode_multipart,
    send_text,
    send_typing as send_typing_action,
    send_voice as send_voice_message,
    set_message_reaction,
)
from .telegram_commands import (
    handle_slash_command,
    parse_slash_command,
)
from .telegram_routing import (
    forward_ident,
    should_process_message as should_process_telegram_message,
)
from voice.video import fused_video_event_text, ingest_video


class TelegramChannel:
    name = "telegram"

    # Cache TTL for `getChatMemberCount` results. Membership changes are rare
    # (humans joining/leaving a group), so 5 minutes amortizes the call across
    # bursts of messages without going stale long enough to matter. A larger
    # TTL is tempting but means a third joiner gets bot-replied for longer.
    _MEMBER_COUNT_TTL_SECONDS = 300.0

    # Telegram update types we want to receive. By default getUpdates only
    # delivers `message` + `edited_message`; opting into `my_chat_member` is
    # how we learn the bot was added to / removed from a chat, and
    # `callback_query` carries the inline-keyboard taps for chat-auth.
    _ALLOWED_UPDATE_TYPES = (
        "message",
        "edited_message",
        "my_chat_member",
        "callback_query",
    )

    # Inline-keyboard callback_data prefixes.
    _AUTH_CALLBACK_PREFIX = "chat_auth:"
    _EMAIL_CALLBACK_PREFIX = "jcemail:"
    _APPROVAL_CALLBACK_PREFIX = "apv:"
    _ACTION_STOP_PREFIX = "act:stop:"
    _ACTION_BG_PREFIX = "act:bg:"

    def __init__(self, instance_dir: Path, cfg: ChannelConfig, log: LogFn):
        self.instance_dir = instance_dir
        self.cfg = cfg
        self.log = log
        self.token = env_value(instance_dir, cfg.token_env)
        self.offset = 0
        # Allow/block sets resolved per-call from `_authority_sets()` so
        # external edits to `ops/gateway.yaml` / `.env` take effect without
        # a gateway restart. The static `self.cfg.chat_ids` is a snapshot
        # from process start; do not consult it directly on the auth path.
        self.bot_username: str | None = None
        self.bot_user_id: int | None = None
        self._member_count_cache: dict[str, tuple[int, float]] = {}
        # Cached SQLite connection for chats reads/writes — opened lazily.
        # Single-threaded poller, single-threaded callback_query handler;
        # one connection is safe and skips per-call init_db churn.
        self._chats_conn = None
        # Track the chat_ids we've already prompted auth for so a flapping
        # `my_chat_member` update doesn't fan out into duplicate prompts.
        self._auth_prompts_sent: set[str] = set()

    def _resolve_bot_username(self) -> None:
        """Populate `bot_username` and `bot_user_id` via `getMe`. Best-effort."""
        if not self.token:
            return
        try:
            data = http_json(
                f"https://api.telegram.org/bot{self.token}/getMe",
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram getMe failed: {exc}")
            return
        if not data.get("ok"):
            self.log(f"telegram getMe failed: {data}")
            return
        result = data.get("result") or {}
        username = result.get("username")
        user_id = result.get("id")
        if isinstance(username, str) and username:
            self.bot_username = username.lower()
        if isinstance(user_id, int):
            self.bot_user_id = user_id
        if self.bot_username:
            self.log(
                f"telegram bot_username resolved as @{self.bot_username} (id={self.bot_user_id})"
            )

    def _get_chats_conn(self):
        """Lazy, cached SQLite connection for chats ops."""
        if self._chats_conn is None:
            self._chats_conn = queue_module.connect(self.instance_dir)
        return self._chats_conn

    # Telegram Bot API `setMessageReaction` only accepts emoji from a curated
    # list (Bot API v6.5+ free reactions). All entries below are verified
    # members of that list and read as "agent is busy / on it".
    _BUSY_EMOJIS = ("👀", "🤔", "✍", "👨‍💻", "🫡", "🤓")
    # `⚡` reads as "running now on a parallel slot" (vs. `👀` = queued behind).
    # Used only when `parallel.max_concurrent > 1`; serial gateways keep the
    # random `_BUSY_EMOJIS` reaction for backward compatibility.
    # `🏃` was the original spec pick but Telegram rejects it (REACTION_INVALID
    # via Bot API setMessageReaction — emoji not in the curated allowed list).
    _RUNNING_EMOJI = "⚡"

    def _busy_react(
        self,
        chat_id: str,
        message_id: int,
        conversation_id: str | None = None,
    ) -> None:
        """React to a message when the gateway is processing another event.

        Checks the queue for any currently-running event. If one exists,
        adds an emoji reaction immediately, then removes it after 30 s via
        a daemon thread. Best-effort throughout — never raises.

        For the default `parallel.max_concurrent: 1` config the reaction is
        a random pick from `_BUSY_EMOJIS` — byte-identical to the pre-
        parallel-slots behavior. For `max_concurrent > 1` the reaction is
        deterministic per slot availability for the current conversation:

          - `⚡` — at least one slot is free, this event will run now.
          - `👀` — all slots are busy, this event will queue.
        """
        max_concurrent = self._max_concurrent()
        try:
            # Fresh connection avoids stale WAL snapshots from _chats_conn's
            # ongoing transaction (upsert_chat writes leave the connection
            # mid-transaction in Python 3.12+ implicit-transaction mode).
            conn = queue_module.connect(self.instance_dir)
            try:
                if max_concurrent > 1 and conversation_id:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n FROM events "
                        "WHERE status='running' AND conversation_id=?",
                        (conversation_id,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n FROM events WHERE status='running'"
                    ).fetchone()
                running = int(row["n"]) if row else 0
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram busy_react query failed: {exc}")
            return
        if running == 0:
            return
        if max_concurrent > 1:
            emoji = self._RUNNING_EMOJI if running < max_concurrent else "👀"
        else:
            emoji = random.choice(self._BUSY_EMOJIS)
        token = self.token
        self.log(
            f"telegram busy_react chat_id={chat_id} message_id={message_id} "
            f"emoji={emoji} running={running} max_concurrent={max_concurrent}"
        )
        set_message_reaction(token=token, chat_id=chat_id, message_id=message_id, emoji=emoji, log=self.log)

        def _remove() -> None:
            time.sleep(30)
            set_message_reaction(token=token, chat_id=chat_id, message_id=message_id, emoji=None)

        threading.Thread(target=_remove, daemon=True).start()

    def _max_concurrent(self) -> int:
        """Return the configured `parallel.max_concurrent` (default 1).

        Best-effort: any config-load failure falls back to 1 so the serial
        path stays the safe default.
        """
        try:
            return int(load_config_cached(self.instance_dir).parallel.max_concurrent)
        except Exception:  # noqa: BLE001
            return 1

    def close(self) -> None:
        """Release cached resources (chats DB connection). Idempotent."""
        if self._chats_conn is not None:
            try:
                self._chats_conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._chats_conn = None

    def __del__(self):  # pragma: no cover - defensive cleanup at interpreter shutdown
        self.close()

    def _main_chat_id(self) -> str | None:
        """Resolve the configured main DM chat_id.

        Auth prompts and replies prefer the env'd `TELEGRAM_CHAT_ID`,
        which doubles as the operator's user id (DM `chat_id == user_id`).
        Newer config-only instances may only carry the operator chat in
        `ops/gateway.yaml`, so fall back to the first configured allowed
        Telegram chat instead of silently skipping approval prompts.
        """
        env = env_value(self.instance_dir, "TELEGRAM_CHAT_ID")
        if env:
            return str(env)
        try:
            cfg = load_config_cached(self.instance_dir).channel("telegram")
            for chat_id in tuple(cfg.chat_ids) + _env_chat_ids(self.instance_dir):
                if str(chat_id):
                    return str(chat_id)
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram main chat fallback failed: {exc}")
        return None

    def _authority_sets(self) -> tuple[frozenset[str], frozenset[str]]:
        """Return `(allowed, blocked)` chat-id sets from config files.

        Reads `ops/gateway.yaml` (`channels.telegram.chat_ids` +
        `blocked_chat_ids`) and `.env` (`TELEGRAM_CHAT_IDS`). Both
        sources are mtime-cached so the poll loop stays cheap; an
        external edit takes effect on the next call once the OS
        flushes the new mtime.
        """
        cfg = load_config_cached(self.instance_dir).channel("telegram")
        allowed = set(cfg.chat_ids) | set(_env_chat_ids(self.instance_dir))
        blocked = set(cfg.blocked_chat_ids)
        return frozenset(allowed), frozenset(blocked)

    def _is_authorized(self, chat_id: str) -> bool:
        """Decide if a chat may have its messages dispatched to the brain.

        Default-deny. Sources of truth are config files only:
          1. `blocked_chat_ids` in yaml — always reject (overrides allow).
          2. `chat_ids` in yaml or `TELEGRAM_CHAT_IDS` in `.env` — allow.
          3. `TELEGRAM_CHAT_ID` (operator's main DM) — allow.
          4. Otherwise — not authorized; message is dropped.

        No SQLite read on this path. Approvals/rejections live in
        `ops/gateway.yaml` + `.env`, written by `_handle_callback_query`.
        """
        allowed, blocked = self._authority_sets()
        if chat_id in blocked:
            return False
        if chat_id in allowed:
            return True
        main = self._main_chat_id()
        if main and chat_id == main:
            return True
        return False

    def _is_blocked(self, chat_id: str) -> bool:
        """True iff chat_id is on `blocked_chat_ids`. Cheap config-only lookup."""
        _allowed, blocked = self._authority_sets()
        return chat_id in blocked

    def _handle_my_chat_member(self, update: dict) -> None:
        """Handle a `my_chat_member` update — bot membership changed in a chat.

        We care about two transitions:
          - bot added (status `member` or `administrator`) → prompt for auth.
          - bot kicked / left → add chat to `blocked_chat_ids` so future
            re-adds don't reach the operator until explicitly approved.
        DM upgrades / status churn within an already-known chat are ignored.
        """
        mcm = update.get("my_chat_member") or {}
        chat = mcm.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if not chat_id:
            return
        new_member = mcm.get("new_chat_member") or {}
        new_status = new_member.get("status") or ""
        new_user = new_member.get("user") or {}
        if self.bot_user_id and new_user.get("id") != self.bot_user_id:
            # Update is about another user, not us. Telegram fans these out
            # for every member transition; ignore unless it's our own.
            return
        added_by = mcm.get("from") or {}
        chat_type = chat.get("type")
        if new_status in ("member", "administrator"):
            self._handle_bot_added(chat, added_by)
            return
        if new_status in ("left", "kicked"):
            try:
                self._block_chat(chat_id)
            except Exception as exc:  # noqa: BLE001
                self.log(f"telegram block-on-leave failed chat_id={chat_id}: {exc}")
            self.log(
                f"telegram bot left chat_id={chat_id} type={chat_type} "
                f"new_status={new_status} — added to blocked_chat_ids"
            )

    def _handle_bot_added(self, chat: dict, added_by: dict | None = None) -> None:
        """Prompt main DM for approval of a freshly-joined chat.

        Idempotent: if the chat is already on the yaml allowlist, in a
        DM, on the blocklist, or already prompted this process, we do
        not re-prompt. Auth state lives in config files only — we do
        record an observability row in the chats table but no
        `auth_status` is set there.
        """
        chat_id = str(chat.get("id", ""))
        if not chat_id:
            return
        allowed, blocked = self._authority_sets()
        if chat_id in allowed or chat_id in blocked:
            return
        chat_type = chat.get("type")
        if chat_type == "private":
            return  # operator's own DM is implicitly allowed
        # DM with the operator slips through here too.
        main = self._main_chat_id()
        if main and chat_id == main:
            return
        title = chat.get("title") or "(untitled)"
        member_count = self._cached_member_count(chat_id)
        try:
            chats_module.upsert_chat(
                conn=self._get_chats_conn(),
                channel="telegram",
                chat_id=chat_id,
                chat_type=chat_type,
                title=title,
                username=chat.get("username"),
                member_count=member_count,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram bot-added upsert failed chat_id={chat_id}: {exc}")
        # Suppress duplicate prompts within this process lifetime.
        if chat_id in self._auth_prompts_sent:
            return
        if self._send_auth_prompt(chat, added_by or {}):
            self._auth_prompts_sent.add(chat_id)

    def _send_auth_prompt(
        self,
        chat: dict,
        added_by: dict | None = None,
        message_preview: str | None = None,
    ) -> bool:
        """Send an inline-keyboard Allow/Deny prompt to the main DM.

        Best-effort — failure is logged and returns False so a later
        inbound message can retry the prompt. The operator can still
        allow/deny via `jc chats approve <chat_id>`.

        Args:
            chat: Telegram chat dict (id, type, title, username, etc.)
            added_by: Optional dict with username/first_name for group-add context.
                      If None, formats as "new sender" instead of "bot added".
            message_preview: Optional text snippet from the new sender.
        """
        main = self._main_chat_id()
        if not main or not self.token:
            self.log("telegram auth prompt skipped: no main chat or token")
            return False
        chat_id = str(chat.get("id", ""))
        title = chat.get("title") or "(untitled)"
        chat_type = chat.get("type") or "?"
        member_count = self._cached_member_count(chat_id)
        member_blurb = (
            f"{member_count} members" if member_count is not None else chat_type
        )

        # Branch on context: group-add vs new sender
        if added_by:
            # Group-add context (existing behavior)
            adder = (
                added_by.get("username")
                and f"@{added_by['username']}"
            ) or (
                added_by.get("first_name") or "(unknown)"
            )
            body = (
                "Bot added to a new chat - approve?\n\n"
                f"{title} ({chat_type}, {member_blurb})\n"
                f"chat_id: {chat_id}\n"
                f"added by: {adder}\n\n"
                "Tap Allow to start processing messages from this chat."
            )
        else:
            # New sender DM context
            username = chat.get("username")
            sender_handle = f"@{username}" if username else f"user {chat_id}"
            preview_blurb = ""
            if message_preview:
                # Escape and truncate preview
                escaped = message_preview[:100].replace("\n", " ").strip()
                if len(message_preview) > 100:
                    escaped += "…"
                preview_blurb = f'Preview: "{escaped}"\n\n'
            body = (
                "New contact\n\n"
                f"{sender_handle} ({chat_type})\n"
                f"chat_id: {chat_id}\n"
                f"{preview_blurb}"
                "Tap Allow to process messages from this sender."
            )
        keyboard = {
            "inline_keyboard": [[
                {
                    "text": "✅ Allow",
                    "callback_data": f"{self._AUTH_CALLBACK_PREFIX}allow:{chat_id}",
                },
                {
                    "text": "⛔ Deny + leave",
                    "callback_data": f"{self._AUTH_CALLBACK_PREFIX}deny:{chat_id}",
                },
            ]]
        }
        payload: dict[str, Any] = {
            "chat_id": main,
            "text": body,
            "disable_web_page_preview": True,
            "reply_markup": json.dumps(keyboard),
        }
        try:
            data = http_json(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data=payload,
                timeout=15,
            )
            if not data.get("ok"):
                self.log(f"telegram auth prompt rejected: {data}")
                return False
            self.log(
                f"telegram auth prompt sent chat_id={chat_id} title={title!r}"
            )
            self._mirror_sender_approval(
                chat_id=chat_id,
                chat=chat,
                added_by=added_by,
                message_preview=message_preview,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram auth prompt failed chat_id={chat_id}: {exc}")
            return False

    def _mirror_sender_approval(
        self,
        *,
        chat_id: str,
        chat: dict,
        added_by: dict | None,
        message_preview: str | None,
    ) -> None:
        """Best-effort: register the sender prompt in the unified approvals table."""
        try:
            from approvals.service import find_by_source, raise_
        except Exception:
            return
        kind = "group_authorize" if added_by else "sender_authorize"
        source_ref = (
            f"{'group' if added_by else 'sender'}:{chat_id}"
        )
        try:
            if find_by_source(self.instance_dir, "gateway:telegram", source_ref):
                return
            chat_type = chat.get("type") or "?"
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "chat_type": chat_type,
                "title": chat.get("title") or "",
                "username": chat.get("username") or "",
                "message_preview": message_preview or "",
                "member_count": self._cached_member_count(chat_id),
            }
            if added_by:
                payload["added_by"] = (
                    added_by.get("username") or added_by.get("first_name") or ""
                )
            raise_(
                self.instance_dir,
                kind=kind,
                title=f"{kind}: {chat.get('title') or chat_id}",
                payload=payload,
                callback_payload={"chat_id": chat_id, "leave_on_reject": True},
                producer="gateway:telegram",
                source_ref=source_ref,
                notify_telegram=False,
                notify_email=False,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"approvals mirror raise failed chat_id={chat_id}: {exc}")

    def _maybe_send_sender_approval_prompt(
        self,
        chat_id: str,
        chat: dict,
        message: dict,
    ) -> None:
        """Send approval prompt for a new unauthorized sender.

        Only sends if:
        - Chat is not already on yaml `chat_ids` or `blocked_chat_ids`
        - We haven't already sent a prompt for this chat in this process
        - Main DM and token are available

        Does not enqueue or log transcript — that only happens after approval.
        """
        # Suppress duplicate prompts within this process lifetime
        # (even if the process restarts, we'll re-prompt new senders).
        if chat_id in self._auth_prompts_sent:
            return
        allowed, blocked = self._authority_sets()
        if chat_id in allowed or chat_id in blocked:
            return

        # Extract message preview for context
        text = message.get("text") or message.get("caption") or ""
        preview = text[:100].replace("\n", " ").strip() if text else "(no text)"

        # Send the prompt. Only suppress future prompts after Telegram
        # confirms delivery; a rejected/skipped prompt must stay retryable.
        if self._send_auth_prompt(chat, added_by=None, message_preview=preview):
            self._auth_prompts_sent.add(chat_id)

    def _handle_callback_query(self, update: dict) -> None:
        """Process an inline-keyboard tap. Handles chat_auth: / jcemail: / apv: / act: prefixes."""
        cq = update.get("callback_query") or {}
        cq_id = cq.get("id")
        data = cq.get("data") or ""
        from_user = cq.get("from") or {}
        msg = cq.get("message") or {}
        if data.startswith(self._EMAIL_CALLBACK_PREFIX):
            self._handle_email_callback(cq_id, data, from_user, msg)
            return
        if data.startswith(self._APPROVAL_CALLBACK_PREFIX):
            self._handle_approval_callback(cq_id, data, from_user, msg)
            return
        if data.startswith(self._ACTION_STOP_PREFIX):
            self._handle_action_stop(
                cq_id, data[len(self._ACTION_STOP_PREFIX):], from_user, msg
            )
            return
        if data.startswith(self._ACTION_BG_PREFIX):
            self._handle_action_background(
                cq_id, data[len(self._ACTION_BG_PREFIX):], from_user, msg
            )
            return
        if not data.startswith(self._AUTH_CALLBACK_PREFIX):
            return
        # Only the operator may authorize.
        main = self._main_chat_id()
        if main and str(from_user.get("id", "")) != main:
            self._answer_callback(cq_id, "not authorized")
            self.log(
                f"telegram auth callback rejected: from={from_user.get('id')} "
                f"main={main}"
            )
            return
        try:
            _, action, target_chat_id = data.split(":", 2)
        except ValueError:
            self._answer_callback(cq_id, "bad payload")
            return
        if action not in ("allow", "deny"):
            self._answer_callback(cq_id, "bad action")
            return
        try:
            title = self._cached_title(target_chat_id) or target_chat_id
            if action == "allow":
                self._approve_chat(target_chat_id)
            else:
                self._block_chat(target_chat_id)
        except Exception as exc:  # noqa: BLE001
            self.log(
                f"telegram auth write failed chat_id={target_chat_id} "
                f"action={action}: {exc}"
            )
            self._answer_callback(cq_id, "write failed")
            return
        if action == "deny":
            self._leave_chat(target_chat_id)
        self._mirror_chat_auth_decision(target_chat_id, action, from_user)
        # Edit the original prompt to reflect the decision.
        edit_text = (
            f"✅ Allowed — {title}"
            if action == "allow"
            else f"⛔ Denied + left — {title}"
        )
        self._edit_message_text(
            chat_id=msg.get("chat", {}).get("id"),
            message_id=msg.get("message_id"),
            text=edit_text,
        )
        self._answer_callback(
            cq_id,
            "allowed" if action == "allow" else "denied",
        )
        self.log(
            f"telegram auth flipped chat_id={target_chat_id} action={action} "
            f"(config-only)"
        )

    def _handle_action_stop(
        self,
        cq_id: Any,
        short_token: str,
        from_user: dict,
        msg: dict,
    ) -> None:
        """Stop the brain child for the session bound to ``short_token``.

        Authorization: any ``chat_auth``-approved chat_id (caller's user_id /
        in DMs equals the chat_id). Per spec §4.7, not operator-only.
        """
        from .. import actions, actions_registry

        if not short_token:
            self._answer_callback(cq_id, "bad payload")
            return
        from_id = str(from_user.get("id", ""))
        if not from_id or not self._is_authorized(from_id):
            self._answer_callback(cq_id, "not authorized")
            self.log(
                f"telegram action stop rejected: from={from_id} token={short_token}"
            )
            return
        entry = actions_registry.resolve(short_token)
        if entry is None:
            self._answer_callback(cq_id, "session already ended")
            self.log(f"telegram action stop unknown token={short_token}")
            return

        if actions_registry.check_and_set_debounce(entry.session_id):
            self._answer_callback(cq_id, "")
            return

        # ack the tap before doing real work — Telegram drops the spinner once
        # answered, so the user gets feedback even if the kill takes ~5s.
        self._answer_callback(cq_id, "Stopping…")

        grace = self._action_stop_grace_seconds()
        result = actions.stop_session(
            entry.session_id,
            stop_grace_seconds=grace,
            instance_dir=self.instance_dir,
            actor_chat_id=from_id,
        )

        suffix = self._stopped_suffix(entry, result, language=msg)
        original = entry.card_text or self._extract_message_text(msg) or ""
        new_text = (original.rstrip() + "\n\n" + suffix) if original else suffix
        self._edit_card_after_stop(
            chat_id=msg.get("chat", {}).get("id"),
            message_id=msg.get("message_id"),
            text=new_text,
        )
        self.log(
            f"telegram action stop session={entry.session_id[:12]} "
            f"pid={entry.child_pid} ok={result.ok} "
            f"already_stopped={result.already_stopped} reason={result.reason} "
            f"elapsed_ms={result.elapsed_ms}"
        )

    def _handle_action_background(
        self,
        cq_id: Any,
        short_token: str,
        from_user: dict,
        msg: dict,
    ) -> None:
        """Demote the session bound to ``short_token`` to background.

        The brain subprocess keeps running; the inbound router immediately
        spawns a fresh primary for the chat. The runtime intercepts the
        backgrounded session's final reply as a "Background done" card.
        """
        from .. import actions, actions_registry

        if not short_token:
            self._answer_callback(cq_id, "bad payload")
            return
        from_id = str(from_user.get("id", ""))
        if not from_id or not self._is_authorized(from_id):
            self._answer_callback(cq_id, "not authorized")
            self.log(
                f"telegram action background rejected: from={from_id} token={short_token}"
            )
            return
        entry = actions_registry.resolve(short_token)
        if entry is None:
            self._answer_callback(cq_id, "session already ended")
            self.log(f"telegram action background unknown token={short_token}")
            return

        if actions_registry.check_and_set_debounce(entry.session_id):
            self._answer_callback(cq_id, "")
            return

        chat_id = str(
            entry.chat_id
            or (msg.get("chat") or {}).get("id")
            or ""
        )
        supervisor_msg_id = entry.supervisor_msg_id or msg.get("message_id")
        max_per_chat = self._action_max_background_per_chat()

        result = actions.background_session(
            entry.session_id,
            chat_id=chat_id,
            supervisor_msg_id=int(supervisor_msg_id) if supervisor_msg_id else None,
            max_per_chat=max_per_chat,
            instance_dir=self.instance_dir,
            actor_chat_id=from_id,
        )

        if result.capped:
            self._answer_callback(cq_id, "Limit reached")
            self.log(
                f"telegram action background capped: token={short_token} "
                f"chat={chat_id} max={max_per_chat}"
            )
            return
        if result.already_backgrounded:
            self._answer_callback(cq_id, "Already done")
            return
        if not result.ok:
            self._answer_callback(cq_id, "failed")
            self.log(
                f"telegram action background failed: token={short_token} "
                f"reason={result.reason}"
            )
            return

        self._answer_callback(cq_id, "Backgrounded")

        suffix = self._backgrounded_suffix()
        original = entry.card_text or self._extract_message_text(msg) or ""
        new_text = (original.rstrip() + "\n\n" + suffix) if original else suffix
        self._edit_card_after_background(
            chat_id=(msg.get("chat") or {}).get("id") or chat_id,
            message_id=msg.get("message_id") or supervisor_msg_id,
            text=new_text,
        )
        self.log(
            f"telegram action background session={entry.session_id[:12]} "
            f"pid={entry.child_pid} chat={chat_id} "
            f"elapsed_ms={result.elapsed_ms}"
        )

    def _action_max_background_per_chat(self) -> int:
        """Resolve ``gateway.actions.max_background_per_chat``. Default 3."""
        try:
            return int(load_config_cached(self.instance_dir).actions.max_background_per_chat)
        except Exception:  # noqa: BLE001
            return 3

    @staticmethod
    def _backgrounded_suffix() -> str:
        """Build the trailing ``🔄 Backgrounded at HH:MM:SS UTC`` line."""
        from datetime import datetime, timezone

        hhmmss = datetime.now(timezone.utc).strftime("%H:%M:%S")
        return f"🔄 Backgrounded at {hhmmss} UTC"

    @staticmethod
    def _backgrounded_running_keyboard() -> dict:
        """Single disabled-style button replacing the Stop/Background row."""
        return {
            "inline_keyboard": [[
                {
                    "text": "🔄 Backgrounded · running",
                    # Telegram inline buttons must carry callback_data (or a
                    # url / switch_inline_query). The token is dead — the
                    # callback handler tolerates unknown tokens with a
                    # "session already ended" answer, so a stale press is
                    # cleanly no-op.
                    "callback_data": "act:bg:done",
                },
            ]]
        }

    def _edit_card_after_background(
        self,
        *,
        chat_id: Any,
        message_id: Any,
        text: str,
    ) -> None:
        """Edit the supervisor card to ``Backgrounded · running`` state."""
        if not chat_id or not message_id or not self.token:
            return
        keyboard = json.dumps(self._backgrounded_running_keyboard())
        try:
            data = http_json(
                f"https://api.telegram.org/bot{self.token}/editMessageText",
                data={
                    "chat_id": str(chat_id),
                    "message_id": int(message_id),
                    "text": to_markdown_v2(text),
                    "parse_mode": "MarkdownV2",
                    "disable_web_page_preview": True,
                    "reply_markup": keyboard,
                },
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram editMessageText (background) failed: {exc}")
            return
        if data.get("ok"):
            return
        description = str(data.get("description") or "").lower()
        if "not modified" in description:
            return
        if "parse" in description or "entit" in description:
            try:
                fallback = http_json(
                    f"https://api.telegram.org/bot{self.token}/editMessageText",
                    data={
                        "chat_id": str(chat_id),
                        "message_id": int(message_id),
                        "text": text,
                        "disable_web_page_preview": True,
                        "reply_markup": keyboard,
                    },
                    timeout=10,
                )
                if fallback.get("ok"):
                    return
                self.log(
                    f"telegram editMessageText (background) plain-fallback failed: {fallback}"
                )
            except Exception as exc:  # noqa: BLE001
                self.log(
                    f"telegram editMessageText (background) plain-fallback error: {exc}"
                )
            return
        self.log(f"telegram editMessageText (background) not ok: {data}")

    def _action_stop_grace_seconds(self) -> int:
        """Resolve ``gateway.actions.stop_grace_seconds`` from config. Default 5."""
        try:
            return int(load_config_cached(self.instance_dir).actions.stop_grace_seconds)
        except Exception:  # noqa: BLE001
            return 5

    @staticmethod
    def _stopped_suffix(entry: Any, result: Any, language: dict | None = None) -> str:
        """Build the trailing ``✋ Stopped at HH:MM:SS UTC · MM:SS`` line."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        hhmmss = now.strftime("%H:%M:%S")
        duration_seconds = max(0, int(time.time() - float(entry.started_at)))
        mm = duration_seconds // 60
        ss = duration_seconds % 60
        duration_str = f"{mm:02d}:{ss:02d}"
        return f"✋ Stopped at {hhmmss} UTC · {duration_str}"

    @staticmethod
    def _extract_message_text(msg: dict) -> str:
        """Best-effort: read the ``text`` field from a callback's message payload."""
        if not isinstance(msg, dict):
            return ""
        text = msg.get("text") or msg.get("caption") or ""
        return text if isinstance(text, str) else ""

    def _edit_card_after_stop(
        self,
        *,
        chat_id: Any,
        message_id: Any,
        text: str,
    ) -> None:
        """Edit the supervisor card to its stopped form and drop the keyboard."""
        if not chat_id or not message_id or not self.token:
            return
        try:
            data = http_json(
                f"https://api.telegram.org/bot{self.token}/editMessageText",
                data={
                    "chat_id": str(chat_id),
                    "message_id": int(message_id),
                    "text": to_markdown_v2(text),
                    "parse_mode": "MarkdownV2",
                    "disable_web_page_preview": True,
                    "reply_markup": json.dumps({"inline_keyboard": []}),
                },
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram editMessageText (stop) failed: {exc}")
            return
        if data.get("ok"):
            return
        description = str(data.get("description") or "").lower()
        if "not modified" in description:
            return
        # Parse-error fallback: retry without MarkdownV2.
        if "parse" in description or "entit" in description:
            try:
                fallback = http_json(
                    f"https://api.telegram.org/bot{self.token}/editMessageText",
                    data={
                        "chat_id": str(chat_id),
                        "message_id": int(message_id),
                        "text": text,
                        "disable_web_page_preview": True,
                        "reply_markup": json.dumps({"inline_keyboard": []}),
                    },
                    timeout=10,
                )
                if fallback.get("ok"):
                    return
                self.log(f"telegram editMessageText (stop) plain-fallback failed: {fallback}")
            except Exception as exc:  # noqa: BLE001
                self.log(f"telegram editMessageText (stop) plain-fallback error: {exc}")
            return
        self.log(f"telegram editMessageText (stop) not ok: {data}")

    def _handle_approval_callback(
        self, cq_id: Any, data: str, from_user: dict, msg: dict
    ) -> None:
        """Route `apv:` callback taps to the unified approvals adapter."""
        from approvals.channels import telegram as approvals_tg

        result = approvals_tg.handle_callback_query(
            self.instance_dir,
            callback_data=data,
            from_user_id=str(from_user.get("id", "")),
        )
        if not result.get("ok"):
            error = result.get("error", "error")
            self._answer_callback(cq_id, error[:60])
            self.log(f"approvals callback {error}: data={data}")
            return
        record = result["approval"]
        action = result["action"]
        label = "✅ Approved" if action == "approve" else "❌ Rejected"
        text = f"{label} — {record.kind} ({record.short_id})"
        self._edit_message_text(
            chat_id=msg.get("chat", {}).get("id"),
            message_id=msg.get("message_id"),
            text=text,
        )
        self._answer_callback(cq_id, "approved" if action == "approve" else "rejected")
        self.log(
            f"approvals decided id={record.approval_id[:8]} kind={record.kind} "
            f"action={action} status={record.status}"
        )

    def _mirror_chat_auth_decision(
        self, chat_id: str, action: str, from_user: dict
    ) -> None:
        """Resolve the unified approval row a legacy chat_auth: tap implies."""
        try:
            from approvals.models import ApprovalConflict, ApprovalNotFound
            from approvals.service import decide, find_by_source
        except Exception:
            return
        decide_action = "approve" if action == "allow" else "reject"
        for source_ref in (f"sender:{chat_id}", f"group:{chat_id}"):
            try:
                existing = find_by_source(
                    self.instance_dir, "gateway:telegram", source_ref
                )
                if existing is None or existing.status != "pending":
                    continue
                try:
                    decide(
                        self.instance_dir,
                        existing.approval_id,
                        action=decide_action,
                        decided_by=f"tg:{from_user.get('id', '')}",
                        decision_channel="telegram",
                        run_callback=False,
                    )
                except (ApprovalConflict, ApprovalNotFound):
                    continue
            except Exception as exc:  # noqa: BLE001
                self.log(f"approvals mirror decide failed chat_id={chat_id}: {exc}")

    def _handle_email_callback(
        self, cq_id: Any, data: str, from_user: dict, msg: dict
    ) -> None:
        """Handle jcemail: inline-keyboard taps for email draft approval."""
        main = self._main_chat_id()
        if main and str(from_user.get("id", "")) != main:
            self._answer_callback(cq_id, "not authorized")
            return
        try:
            _, action, draft_id = data.split(":", 2)
        except ValueError:
            self._answer_callback(cq_id, "bad payload")
            return
        if action not in ("approve", "reject"):
            self._answer_callback(cq_id, "bad action")
            return
        import shutil
        jc_email = shutil.which("jc-email") or str(
            Path(__file__).resolve().parents[3] / "bin" / "jc-email"
        )
        env = __import__("os").environ.copy()
        env["JC_INSTANCE_DIR"] = str(self.instance_dir)
        try:
            proc = __import__("subprocess").run(
                [jc_email, "drafts", action, draft_id],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            ok = proc.returncode == 0
        except Exception as exc:  # noqa: BLE001
            self.log(f"email draft {action} failed: {exc}")
            self._answer_callback(cq_id, "error")
            return
        if not ok:
            self.log(f"jc-email drafts {action} {draft_id} rc={proc.returncode} err={proc.stderr[:200]}")
            self._answer_callback(cq_id, "failed")
            return
        label = "✅ Approved" if action == "approve" else "❌ Rejected"
        self._edit_message_text(
            chat_id=msg.get("chat", {}).get("id"),
            message_id=msg.get("message_id"),
            text=f"{label} — {draft_id}",
        )
        self._answer_callback(cq_id, "approved" if action == "approve" else "rejected")
        self.log(f"email draft {action} draft_id={draft_id}")

    def _approve_chat(self, chat_id: str) -> None:
        """Add `chat_id` to yaml `chat_ids` + `.env` `TELEGRAM_CHAT_IDS`.

        Idempotent. Also removes `chat_id` from `blocked_chat_ids` if
        present so a previously-rejected chat can be re-approved.
        """
        update_gateway_yaml_chat_lists(
            self.instance_dir,
            channel="telegram",
            allow_add=[chat_id],
            block_remove=[chat_id],
        )
        update_env_chat_ids(self.instance_dir, add=[chat_id])
        # Bust the config cache so the next poll picks up the change
        # immediately without waiting for an mtime tick.
        from ..config import clear_config_cache

        clear_config_cache()

    def _block_chat(self, chat_id: str) -> None:
        """Add `chat_id` to yaml `blocked_chat_ids`. Removes from `chat_ids`.

        Idempotent. `.env` is intentionally NOT touched — rejection
        lives in yaml only, since `TELEGRAM_CHAT_IDS` is an allowlist.
        """
        update_gateway_yaml_chat_lists(
            self.instance_dir,
            channel="telegram",
            block_add=[chat_id],
            allow_remove=[chat_id],
        )
        # Also strip from .env in case it was previously approved.
        update_env_chat_ids(self.instance_dir, remove=[chat_id])
        from ..config import clear_config_cache

        clear_config_cache()

    def _cached_title(self, chat_id: str) -> str | None:
        """Best-effort title lookup from the chats DB for prompt rendering.

        DB stays the audit/observability store; this is a read-only
        peek for nicer prompt text. Falls back to None silently.
        """
        try:
            row = chats_module.get_chat(
                conn=self._get_chats_conn(),
                channel="telegram",
                chat_id=chat_id,
            )
        except Exception:  # noqa: BLE001
            return None
        return row.title if row is not None else None

    def _answer_callback(self, callback_query_id: Any, text: str) -> None:
        if not callback_query_id or not self.token:
            return
        try:
            http_json(
                f"https://api.telegram.org/bot{self.token}/answerCallbackQuery",
                data={"callback_query_id": str(callback_query_id), "text": text},
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram answerCallbackQuery failed: {exc}")

    def _edit_message_text(
        self,
        *,
        chat_id: Any,
        message_id: Any,
        text: str,
    ) -> None:
        if not chat_id or not message_id or not self.token:
            return
        try:
            data = http_json(
                f"https://api.telegram.org/bot{self.token}/editMessageText",
                data={
                    "chat_id": str(chat_id),
                    "message_id": int(message_id),
                    "text": to_markdown_v2(text),
                    "parse_mode": "MarkdownV2",
                    "reply_markup": json.dumps({"inline_keyboard": []}),
                },
                timeout=10,
            )
            # The API response used to be discarded (audit F-P2): a not-ok
            # edit means an approval card can keep live buttons after the
            # decision was applied. "message is not modified" is benign.
            if isinstance(data, dict) and not data.get("ok"):
                desc = str(data.get("description") or "")
                if "not modified" not in desc.lower():
                    self.log(
                        f"telegram editMessageText not-ok chat_id={chat_id} "
                        f"message_id={message_id}: {data}"
                    )
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram editMessageText failed: {exc}")

    def _leave_chat(self, chat_id: str) -> None:
        if not chat_id or not self.token:
            return
        try:
            data = http_json(
                f"https://api.telegram.org/bot{self.token}/leaveChat",
                data={"chat_id": chat_id},
                timeout=10,
            )
            if not data.get("ok"):
                self.log(f"telegram leaveChat not-ok chat_id={chat_id}: {data}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram leaveChat failed chat_id={chat_id}: {exc}")

    def _should_process_message(self, message: dict) -> bool:
        return should_process_telegram_message(
            message,
            bot_username=self.bot_username,
            bot_user_id=self.bot_user_id,
            get_chat_member_count=self._get_chat_member_count,
        )

    def _get_chat_member_count(self, chat_id: str) -> int | None:
        """Return cached `getChatMemberCount` for `chat_id`, or fetch + cache.

        Best-effort: returns `None` on HTTP errors / non-OK responses so
        callers can fall through to the @-mention check. First successful
        fetch logs once per cache fill so 1:1 detection is auditable.
        """
        if not self.token:
            return None
        now = time.monotonic()
        cached = self._member_count_cache.get(chat_id)
        if cached is not None and cached[1] > now:
            return cached[0]
        try:
            data = http_json(
                f"https://api.telegram.org/bot{self.token}/getChatMemberCount?"
                + urllib.parse.urlencode({"chat_id": chat_id}),
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram getChatMemberCount failed chat_id={chat_id}: {exc}")
            return None
        if not data.get("ok"):
            self.log(f"telegram getChatMemberCount not-ok chat_id={chat_id}: {data}")
            return None
        count = data.get("result")
        if not isinstance(count, int):
            return None
        self._member_count_cache[chat_id] = (count, now + self._MEMBER_COUNT_TTL_SECONDS)
        if count <= 2:
            self.log(
                f"telegram group is 1:1 with bot count={count} chat_id={chat_id} — process all"
            )
        return count

    def ready(self) -> bool:
        return bool(self.token)

    # Exponential backoff for consecutive not-ok getUpdates responses:
    # 5, 10, 20, 40, 80, 160, capped at 300 seconds.
    _POLL_BACKOFF_BASE_SECONDS = 5.0
    _POLL_BACKOFF_MAX_SECONDS = 300.0

    def _handle_poll_not_ok(
        self,
        data: dict[str, Any],
        *,
        streak: int,
        should_stop: Callable[[], bool],
    ) -> None:
        """Log + back off after a not-ok getUpdates body (409/429 et al).

        409 means another poller is consuming this token — usually a sibling
        instance that leaked TELEGRAM_BOT_TOKEN (the fleet's known
        impersonation vector); it gets a dedicated loud log line. 429 honors
        ``parameters.retry_after`` when it exceeds the computed backoff.
        """
        error_code = data.get("error_code")
        description = data.get("description") or ""
        parameters = data.get("parameters") or {}
        retry_after = parameters.get("retry_after")
        delay = min(
            self._POLL_BACKOFF_MAX_SECONDS,
            self._POLL_BACKOFF_BASE_SECONDS * (2 ** max(0, streak - 1)),
        )
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            delay = max(delay, float(retry_after))
        if error_code == 409:
            self.log(
                f"telegram getUpdates conflict (409): another client is polling "
                f"this token — possible cross-instance token bleed. "
                f"desc={description!r} streak={streak} backoff={delay:.0f}s"
            )
        else:
            self.log(
                f"telegram getUpdates not-ok error_code={error_code} "
                f"desc={description!r} retry_after={retry_after} "
                f"streak={streak} backoff={delay:.0f}s"
            )
        self._interruptible_sleep(delay, should_stop)

    @staticmethod
    def _interruptible_sleep(
        seconds: float, should_stop: Callable[[], bool]
    ) -> None:
        """Sleep in 1s slices so a long backoff doesn't block shutdown."""
        deadline = time.monotonic() + seconds
        while not should_stop():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def run(self, enqueue: EnqueueFn, should_stop: Callable[[], bool]) -> None:
        if not self.ready():
            self.log("telegram disabled: token missing")
            return
        self.log("telegram poller started")
        # Consecutive not-ok getUpdates responses. `http_json` returns API
        # error bodies as parsed JSON (42582e1 — required for the 400
        # no-op-edit dedup), so a 409 (token conflict — another poller on
        # this token, the cross-instance bleed signature) or 429 arrives
        # here as `{"ok": false}` instead of an exception. Without an
        # explicit check the loop re-polls instantly, invisibly, forever.
        poll_not_ok_streak = 0
        try:
            while not should_stop():
                try:
                    url = f"https://api.telegram.org/bot{self.token}/getUpdates"
                    params = urllib.parse.urlencode(
                        {
                            "timeout": self.cfg.timeout_seconds,
                            "offset": self.offset,
                            "allowed_updates": json.dumps(
                                list(self._ALLOWED_UPDATE_TYPES)
                            ),
                        }
                    )
                    data = http_json(f"{url}?{params}", timeout=self.cfg.timeout_seconds + 5)
                    if not data.get("ok", False):
                        poll_not_ok_streak += 1
                        self._handle_poll_not_ok(
                            data, streak=poll_not_ok_streak, should_stop=should_stop
                        )
                        continue
                    poll_not_ok_streak = 0
                    for update in data.get("result", []):
                        if self.bot_username is None and self.token:
                            self._resolve_bot_username()
                        self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
                        # Route non-message update types first.
                        if "my_chat_member" in update:
                            self._handle_my_chat_member(update)
                            continue
                        if "callback_query" in update:
                            self._handle_callback_query(update)
                            continue
                        message = update.get("message") or update.get("edited_message")
                        if not isinstance(message, dict):
                            continue
                        chat = message.get("chat") or {}
                        chat_id = str(chat.get("id", ""))
                        # Hard short-circuit on the operator blocklist before
                        # any record/audit/HTTP work. Blocked traffic must not
                        # touch the chats DB or the brain.
                        if chat_id and self._is_blocked(chat_id):
                            self.log(
                                f"telegram dropped blocked chat_id={chat_id} "
                                f"type={chat.get('type')}"
                            )
                            continue
                        # Backstop bot-added detection: some clients only emit
                        # `new_chat_members` on a service message.
                        new_members = message.get("new_chat_members")
                        if isinstance(new_members, list) and self.bot_user_id:
                            for m in new_members:
                                if isinstance(m, dict) and m.get("id") == self.bot_user_id:
                                    self._handle_bot_added(chat, message.get("from"))
                                    break
                        # Run the routing decision first — it populates the
                        # member-count cache that _record_chat reads from.
                        should_process = self._should_process_message(message)
                        # Observability: record every inbound chat (even
                        # unauthorized ones) so the operator can see who
                        # tried to message. Auth state lives in config files.
                        self._record_chat(chat, message)
                        if not self._is_authorized(chat_id):
                            self.log(
                                f"telegram ignored unauthorized chat_id={chat_id} "
                                f"type={chat.get('type')}"
                            )
                            # Send approval prompt for new pending senders
                            # (not for explicitly denied or already-prompted chats).
                            self._maybe_send_sender_approval_prompt(
                                chat_id=chat_id,
                                chat=chat,
                                message=message,
                            )
                            continue
                        if not should_process:
                            self.log(
                                f"telegram ignored non-mention chat_id={chat_id} type={chat.get('type')}"
                            )
                            continue
                        update_id = update.get("update_id")
                        self._log_forward(message, update_id)
                        text = message.get("text") or message.get("caption") or ""
                        voice = message.get("voice") if isinstance(message.get("voice"), dict) else None
                        audio = message.get("audio") if isinstance(message.get("audio"), dict) else None
                        video_note = (
                            message.get("video_note")
                            if isinstance(message.get("video_note"), dict)
                            else None
                        )
                        video = message.get("video") if isinstance(message.get("video"), dict) else None
                        attachment = voice or audio or video_note  # audio-only attachments
                        if voice:
                            kind = "voice"
                        elif audio:
                            kind = "audio"
                        elif video_note:
                            kind = "video_note"
                        else:
                            kind = None
                        audio_path: Path | None = None
                        if attachment is not None:
                            caption = text.strip()
                            try:
                                audio_path = self._ingest_audio_attachment(
                                    attachment, kind, update_id
                                )
                                transcript = _transcribe_audio(
                                    audio_path,
                                    instance_dir=self.instance_dir,
                                ).strip()
                            except Exception as exc:  # noqa: BLE001
                                self.log(
                                    f"telegram {kind} ingestion failed update_id={update_id}: {exc}"
                                )
                                continue
                            if transcript:
                                self.log(
                                    f"telegram {kind} transcribed update_id={update_id} chars={len(transcript)}"
                                )
                                text = f"{caption}\n\n[{kind}: {transcript}]" if caption else transcript
                            elif caption:
                                self.log(
                                    f"telegram {kind} transcription empty update_id={update_id}; keeping caption"
                                )
                                text = caption
                            else:
                                self.log(
                                    f"telegram {kind} transcription empty update_id={update_id}"
                                )
                                continue
                        video_path_ingested: Path | None = None
                        video_transcript: str = ""
                        video_visual: str = ""
                        if video is not None:
                            caption = text.strip()
                            if self._video_enabled():
                                fused = ""
                                try:
                                    raw_path = self._ingest_video_attachment(video, update_id)
                                    video_path_ingested = raw_path
                                    vt, vv = ingest_video(raw_path, instance_dir=self.instance_dir)
                                    video_transcript = vt
                                    video_visual = vv
                                    fused = fused_video_event_text(vt, vv).strip()
                                except ValueError as exc:
                                    self.log(
                                        f"telegram video rejected update_id={update_id}: {exc}"
                                    )
                                    try:
                                        send_text(
                                            instance_dir=self.instance_dir,
                                            token=self.token,
                                            response="Video too large (>50 MB). Send a shorter clip.",
                                            meta={
                                                "chat_id": chat_id,
                                                "message_thread_id": message.get("message_thread_id"),
                                            },
                                            log=self.log,
                                        )
                                    except Exception as send_exc:  # noqa: BLE001
                                        self.log(
                                            f"telegram video reject notice failed: {send_exc}"
                                        )
                                    continue
                                except Exception as exc:  # noqa: BLE001
                                    self.log(
                                        f"telegram video ingestion failed update_id={update_id}: {exc}"
                                    )
                                if not fused:
                                    fused = "[video]"
                                text = f"{caption}\n\n[video: {fused}]" if caption else fused
                            else:
                                text = f"{caption}\n\n[video]" if caption else "[video]"
                        photo = message.get("photo") if isinstance(message.get("photo"), list) else None
                        document = (
                            message.get("document")
                            if isinstance(message.get("document"), dict)
                            else None
                        )
                        image_path: Path | None = None
                        if photo:
                            try:
                                image_path = self._ingest_photo(photo, update_id)
                            except Exception as exc:  # noqa: BLE001
                                self.log(
                                    f"telegram photo ingestion failed update_id={update_id}: {exc}"
                                )
                        file_path: Path | None = None
                        if document:
                            try:
                                file_path = self._ingest_document(document, update_id)
                            except Exception as exc:  # noqa: BLE001
                                self.log(
                                    f"telegram document ingestion failed update_id={update_id}: {exc}"
                                )
                        has_media = image_path is not None or file_path is not None
                        if not text.strip() and not has_media:
                            continue
                        if not text.strip() and has_media:
                            if image_path is not None and file_path is None:
                                text = "[image]"
                            elif file_path is not None:
                                name = (document or {}).get("file_name") or file_path.name
                                text = f"[document: {name}]"
                        thread_id = message.get("message_thread_id")
                        conversation_id = f"{chat_id}:{thread_id}" if thread_id else chat_id
                        _reply_msg = message.get("reply_to_message") or {}
                        meta: dict[str, Any] = {
                            "chat_id": chat_id,
                            "message_id": message.get("message_id"),
                            "message_thread_id": thread_id,
                            "username": (message.get("from") or {}).get("username"),
                            "chat_type": chat.get("type"),
                            "reply_to_message_id": _reply_msg.get("message_id"),
                            "reply_to_text": _reply_msg.get("text") or _reply_msg.get("caption"),
                            "forward_from_message_id": message.get("forward_from_message_id"),
                            "forward_from_chat_id": message.get("forward_from_chat_id"),
                            "forward_from_username": (message.get("forward_from") or {}).get("username"),
                        }
                        if audio_path is not None:
                            # `was_voice` keeps its name for downstream voice-reply
                            # rendering — the trigger is "user sent something we
                            # transcribed", regardless of whether it was a `voice`
                            # bubble, music clip, or round video.
                            meta["was_voice"] = True
                            meta["audio_path"] = str(audio_path)
                            meta["attachment_kind"] = kind
                        if image_path is not None:
                            meta["image_path"] = str(image_path)
                        if file_path is not None:
                            meta["file_path"] = str(file_path)
                            if document and document.get("file_name"):
                                meta["file_name"] = document.get("file_name")
                        if video_path_ingested is not None:
                            meta["video_path"] = str(video_path_ingested)
                            meta["video_transcript"] = video_transcript
                            meta["video_visual"] = video_visual
                        # Check for slash commands (e.g., /help, /models, /compact).
                        # If it's a command, handle it locally and skip enqueueing.
                        cmd = parse_slash_command(text)
                        if cmd is not None:
                            command, args = cmd
                            self.log(
                                f"telegram slash command detected update_id={update_id} "
                                f"cmd={command}"
                            )
                            handle_slash_command(
                                command=command,
                                args=args,
                                instance_dir=self.instance_dir,
                                token=self.token,
                                meta=meta,
                                log=self.log,
                            )
                            continue
                        try:
                            enqueue(
                                source="telegram",
                                source_message_id=str(update.get("update_id")),
                                user_id=str((message.get("from") or {}).get("id", "")) or None,
                                conversation_id=conversation_id,
                                content=text,
                                meta=meta,
                            )
                            self._enqueue_failures = {}
                        except Exception as enq_exc:  # noqa: BLE001
                            # Offset was advanced at the top of this loop —
                            # without a rewind a transient enqueue error
                            # (sqlite "database is locked") permanently
                            # drops this inbound message (audit F-P2 /
                            # feature 6). Rewind + break so the update is
                            # re-fetched next poll. Poison guard: after 3
                            # consecutive failures on the same update_id,
                            # advance past it with a loud drop log so a
                            # poisonous update can't wedge inbound forever.
                            uid = int(update.get("update_id", 0) or 0)
                            failures = getattr(self, "_enqueue_failures", {})
                            count = int(failures.get(uid, 0)) + 1
                            self._enqueue_failures = {uid: count}
                            if count >= 3:
                                self.log(
                                    f"telegram enqueue failed {count}x "
                                    f"update_id={uid} — DROPPING message: {enq_exc}"
                                )
                                continue
                            self.log(
                                f"telegram enqueue failed update_id={uid} "
                                f"(attempt {count}/3): {enq_exc} — offset "
                                "rewound, will re-poll"
                            )
                            self.offset = uid
                            break
                        # Busy acknowledgement: react if another event is running.
                        _chat_id = str(meta.get("chat_id", ""))
                        _msg_id = meta.get("message_id")
                        if _chat_id and isinstance(_msg_id, int):
                            self._busy_react(
                                _chat_id, _msg_id, conversation_id=conversation_id
                            )
                    self.close()
                except Exception as exc:  # noqa: BLE001
                    self.log(f"telegram poll error: {exc}")
                    time.sleep(5)
        finally:
            self.close()
            self.log("telegram poller stopped")

    def _ingest_audio_attachment(
        self,
        payload: dict[str, Any],
        kind: str | None,
        update_id: Any,
    ) -> Path:
        """Download a transcribable Telegram attachment to `state/voice/inbound/`.

        Handles `voice`, `audio`, and `video_note`. `video_note` is always MP4;
        `audio` carries a free-form mime; `voice` defaults to OGG.
        """
        file_id = payload.get("file_id")
        if not file_id:
            raise RuntimeError(f"{kind or 'attachment'} payload missing file_id")
        return ingest_audio_attachment(
            token=self.token,
            instance_dir=self.instance_dir,
            payload=payload,
            kind=kind,
            update_id=update_id,
        )

    def _ingest_photo(self, photos: list[Any], update_id: Any) -> Path:
        """Download the largest photo size to `state/voice/inbound/photos/`."""
        if not photos:
            raise RuntimeError("photo payload empty")
        largest = photos[-1]
        if not isinstance(largest, dict):
            raise RuntimeError("photo payload malformed")
        return ingest_photo(
            token=self.token,
            instance_dir=self.instance_dir,
            photos=photos,
            update_id=update_id,
        )

    def _ingest_document(self, document: dict[str, Any], update_id: Any) -> Path:
        """Download a Telegram document to `state/voice/inbound/docs/`.

        Preserves the original file extension when present; falls back to a
        MIME-derived extension; finally `.bin` if neither is available.
        """
        return ingest_document(
            token=self.token,
            instance_dir=self.instance_dir,
            document=document,
            update_id=update_id,
        )

    def _video_enabled(self) -> bool:
        """Read `voice.video.enabled` from ops/gateway.yaml. Default False."""
        from gateway.config import _load_raw, config_path

        try:
            raw = _load_raw(config_path(self.instance_dir))
        except Exception:  # noqa: BLE001
            return False
        voice_cfg = raw.get("voice") if isinstance(raw.get("voice"), dict) else {}
        video_cfg = voice_cfg.get("video") if isinstance(voice_cfg.get("video"), dict) else {}
        return bool(video_cfg.get("enabled", False))

    def _ingest_video_attachment(
        self, payload: dict[str, Any], update_id: Any
    ) -> Path:
        """Download a Telegram `video` payload to `state/voice/inbound/` as .mp4."""
        file_id = payload.get("file_id")
        if not file_id:
            raise RuntimeError("video payload missing file_id")
        dest = (
            self.instance_dir / "state" / "voice" / "inbound" / f"{update_id}.mp4"
        )
        return _download_telegram_file(self.token, file_id, dest)

    def _dm_title(self, chat: dict[str, Any]) -> str | None:
        """Compose a DM display name as `first_name + last_name` per spec.

        Falls back to `@username` when neither name field is set.
        """
        return telegram_dm_title(chat)

    def _cached_member_count(self, chat_id: str) -> int | None:
        """Return a cached `getChatMemberCount` if present — never fetch.

        Chat-recording is observability; it must not trigger new HTTP calls
        on top of the message-handling path. If the value is already in the
        cache (typically populated by `_get_chat_member_count` during the
        `_should_process_message` check), use it; otherwise return None and
        let the row's `member_count` stay NULL until the next inbound
        message refreshes it.
        """
        return cached_telegram_member_count(self._member_count_cache, chat_id)

    def _record_chat(self, chat: dict[str, Any], message: dict[str, Any]) -> None:
        """Upsert the inbound chat into the chats directory.

        Wrapped in try/except — chat tracking must never block message
        processing. Failures are logged once per occurrence. Reuses the
        channel's cached SQLite connection to avoid per-message
        connection churn.
        """
        record_telegram_chat(
            instance_dir=self.instance_dir,
            conn_factory=self._get_chats_conn,
            member_count_cache=self._member_count_cache,
            log=self.log,
            chat=chat,
            message=message,
        )

    def _log_forward(self, message: dict[str, Any], update_id: Any) -> None:
        """If the message is a forward, log a single audit line."""
        ident = forward_ident(message)
        if ident is not None:
            self.log(f"telegram forward update_id={update_id} from={ident}")

    def send_typing(
        self,
        chat_id: str,
        message_thread_id: int | None = None,
        action: str = "typing",
    ) -> None:
        """POST `sendChatAction`. Best-effort; no return.

        Telegram displays indicators for ~5s, so callers refresh on a ~4s
        cadence while a long-running operation is in flight.
        """
        if not self.ready() or not chat_id:
            return
        send_typing_action(
            token=self.token,
            chat_id=str(chat_id),
            message_thread_id=message_thread_id,
            action=action,
        )

    def send(self, response: str, meta: dict[str, Any]) -> str | None:
        if not self.ready() or not response.strip():
            return None
        ogg_path = meta.get("synthesized_audio_path")
        if ogg_path:
            try:
                return self.send_voice(str(ogg_path), meta, caption=response)
            except Exception as exc:  # noqa: BLE001
                self.log(f"telegram sendVoice failed, falling back to text: {exc}")
        return send_text(
            instance_dir=self.instance_dir,
            token=self.token,
            response=response,
            meta=meta,
            log=self.log,
        )

    def send_voice(self, ogg_path: str, meta: dict[str, Any], caption: str = "") -> str | None:
        """Upload an OGG/Opus file and post it as a Telegram voice message."""
        if not self.ready():
            return None
        return send_voice_message(
            instance_dir=self.instance_dir,
            token=self.token,
            ogg_path=ogg_path,
            meta=meta,
            caption=caption,
        )
