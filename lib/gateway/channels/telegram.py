"""Telegram long-poll channel."""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .. import chats as chats_module
from .. import queue as queue_module
from ..config import ChannelConfig, env_value
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
)
from .telegram_commands import (
    handle_slash_command,
    parse_slash_command,
)
from .telegram_routing import (
    forward_ident,
    should_process_message as should_process_telegram_message,
)


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

    # Inline-keyboard callback_data prefix for chat-auth Allow/Deny taps.
    _AUTH_CALLBACK_PREFIX = "chat_auth:"

    def __init__(self, instance_dir: Path, cfg: ChannelConfig, log: LogFn):
        self.instance_dir = instance_dir
        self.cfg = cfg
        self.log = log
        self.token = env_value(instance_dir, cfg.token_env)
        self.offset = 0
        self.allowed = set(cfg.chat_ids)
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

        Auth prompts and replies fan out from the env'd `TELEGRAM_CHAT_ID`,
        which doubles as the operator's user id (DM `chat_id == user_id`).
        """
        env = env_value(self.instance_dir, "TELEGRAM_CHAT_ID")
        return str(env) if env else None

    def _is_authorized(self, chat_id: str) -> bool:
        """Decide if a chat may have its messages dispatched to the brain.

        Default-deny. Order:
        1. `cfg.chat_ids` (env allowlist) → always allowed (legacy contract).
        2. DM-with-the-operator → always allowed.
        3. `chats.auth_status == 'allowed'` → allowed.
        4. Otherwise → not authorized; message is dropped.

        Unknown chats (no DB row) and DB lookup failures both fail closed.
        Operator approves new chats explicitly via `set_auth_status`.
        """
        if self.allowed and chat_id in self.allowed:
            return True
        main = self._main_chat_id()
        if main and chat_id == main:
            return True
        try:
            row = chats_module.get_chat(
                conn=self._get_chats_conn(),
                channel="telegram",
                chat_id=chat_id,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram auth lookup failed chat_id={chat_id}: {exc} — fail closed")
            return False
        if row is None:
            return False
        return row.auth_status == "allowed"

    def _handle_my_chat_member(self, update: dict) -> None:
        """Handle a `my_chat_member` update — bot membership changed in a chat.

        We care about three transitions:
          - bot added (status `member` or `administrator`) → mark `pending`
            and prompt for auth (skip if already known + not pending).
          - bot kicked / left → mark `denied` so we stop processing.
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
                chats_module.set_auth_status(
                    conn=self._get_chats_conn(),
                    channel="telegram",
                    chat_id=chat_id,
                    status="denied",
                )
            except Exception as exc:  # noqa: BLE001
                self.log(f"telegram auth set on leave failed chat_id={chat_id}: {exc}")
            self.log(
                f"telegram bot left chat_id={chat_id} type={chat_type} "
                f"new_status={new_status} — auth_status=denied"
            )

    def _handle_bot_added(self, chat: dict, added_by: dict | None = None) -> None:
        """Mark a freshly-joined chat `pending` and prompt main DM for auth.

        Idempotent: if the chat is already on the explicit allowlist, in a
        DM, or recorded as `allowed`/`denied`, we do not re-prompt. Only a
        truly-new chat or one stuck in `pending` triggers a prompt.
        """
        chat_id = str(chat.get("id", ""))
        if not chat_id:
            return
        if self.allowed and chat_id in self.allowed:
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
        existing = None
        try:
            existing = chats_module.get_chat(
                conn=self._get_chats_conn(),
                channel="telegram",
                chat_id=chat_id,
            )
        except Exception:  # noqa: BLE001
            pass
        if existing is not None and existing.auth_status in ("allowed", "denied"):
            # Already decided. Don't reset to pending on subsequent
            # my_chat_member updates (admin promotion, etc.).
            return
        try:
            chats_module.upsert_chat(
                conn=self._get_chats_conn(),
                channel="telegram",
                chat_id=chat_id,
                chat_type=chat_type,
                title=title,
                username=chat.get("username"),
                member_count=member_count,
                auth_status="pending",
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram bot-added upsert failed chat_id={chat_id}: {exc}")
            return
        # Suppress duplicate prompts within this process lifetime.
        if chat_id in self._auth_prompts_sent:
            return
        self._send_auth_prompt(chat, added_by or {})
        self._auth_prompts_sent.add(chat_id)

    def _send_auth_prompt(self, chat: dict, added_by: dict) -> None:
        """Send an inline-keyboard Allow/Deny prompt to the main DM.

        Best-effort — failure is logged and the row stays `pending`. The
        operator can still allow/deny via `jc chats approve <chat_id>`.
        """
        main = self._main_chat_id()
        if not main or not self.token:
            self.log("telegram auth prompt skipped: no main chat or token")
            return
        chat_id = str(chat.get("id", ""))
        title = chat.get("title") or "(untitled)"
        chat_type = chat.get("type") or "?"
        member_count = self._cached_member_count(chat_id)
        member_blurb = (
            f"{member_count} members" if member_count is not None else chat_type
        )
        adder = (
            added_by.get("username")
            and f"@{added_by['username']}"
        ) or (
            added_by.get("first_name") or "(unknown)"
        )
        body = (
            "🤝 Bot added to a new chat — approve?\n\n"
            f"*{title}* ({chat_type}, {member_blurb})\n"
            f"chat_id: `{chat_id}`\n"
            f"added by: {adder}\n\n"
            "Tap Allow to start processing messages from this chat."
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
            "text": to_markdown_v2(body),
            "parse_mode": "MarkdownV2",
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
                return
            self.log(
                f"telegram auth prompt sent chat_id={chat_id} title={title!r}"
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram auth prompt failed chat_id={chat_id}: {exc}")

    def _handle_callback_query(self, update: dict) -> None:
        """Process an inline-keyboard tap. Only chat_auth: prefix is wired."""
        cq = update.get("callback_query") or {}
        cq_id = cq.get("id")
        data = cq.get("data") or ""
        from_user = cq.get("from") or {}
        msg = cq.get("message") or {}
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
        new_status = "allowed" if action == "allow" else "denied"
        row = None
        try:
            row = chats_module.set_auth_status(
                conn=self._get_chats_conn(),
                channel="telegram",
                chat_id=target_chat_id,
                status=new_status,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram auth flip failed chat_id={target_chat_id}: {exc}")
            self._answer_callback(cq_id, "flip failed")
            return
        title = (row.title if row else None) or target_chat_id
        if action == "deny":
            self._leave_chat(target_chat_id)
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
            f"telegram auth flipped chat_id={target_chat_id} action={action}"
        )

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
            http_json(
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

    def run(self, enqueue: EnqueueFn, should_stop: Callable[[], bool]) -> None:
        if not self.ready():
            self.log("telegram disabled: token missing")
            return
        self.log("telegram poller started")
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
                        # tried to message and explicitly approve or deny.
                        # New rows default to auth_status='pending'.
                        self._record_chat(chat, message)
                        if not self._is_authorized(chat_id):
                            self.log(
                                f"telegram ignored unauthorized chat_id={chat_id} "
                                f"type={chat.get('type')}"
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
                        attachment = voice or audio or video_note
                        if voice:
                            kind = "voice"
                        elif audio:
                            kind = "audio"
                        elif video_note:
                            kind = "video_note"
                        else:
                            kind = None
                        audio_path: Path | None = None
                        if not text.strip() and attachment is not None:
                            try:
                                audio_path = self._ingest_audio_attachment(
                                    attachment, kind, update_id
                                )
                                text = _transcribe_audio(audio_path)
                            except Exception as exc:  # noqa: BLE001
                                self.log(
                                    f"telegram {kind} ingestion failed update_id={update_id}: {exc}"
                                )
                                continue
                            if not text.strip():
                                self.log(
                                    f"telegram {kind} transcription empty update_id={update_id}"
                                )
                                continue
                            self.log(
                                f"telegram {kind} transcribed update_id={update_id} chars={len(text)}"
                            )
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
                        meta: dict[str, Any] = {
                            "chat_id": chat_id,
                            "message_id": message.get("message_id"),
                            "message_thread_id": thread_id,
                            "username": (message.get("from") or {}).get("username"),
                            "chat_type": chat.get("type"),
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
                        enqueue(
                            source="telegram",
                            source_message_id=str(update.get("update_id")),
                            user_id=str((message.get("from") or {}).get("id", "")) or None,
                            conversation_id=conversation_id,
                            content=text,
                            meta=meta,
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
    ) -> None:
        """POST `sendChatAction` with `action=typing`. Best-effort; no return.

        Telegram displays the indicator for ~5s, so callers refresh on a
        ~4s cadence while a long-running operation is in flight.
        """
        if not self.ready() or not chat_id:
            return
        send_typing_action(
            token=self.token,
            chat_id=str(chat_id),
            message_thread_id=message_thread_id,
        )

    def send(self, response: str, meta: dict[str, Any]) -> str | None:
        if not self.ready() or not response.strip():
            return None
        ogg_path = meta.get("synthesized_audio_path")
        if ogg_path:
            try:
                return self.send_voice(str(ogg_path), meta)
            except Exception as exc:  # noqa: BLE001
                self.log(f"telegram sendVoice failed, falling back to text: {exc}")
        return send_text(
            instance_dir=self.instance_dir,
            token=self.token,
            response=response,
            meta=meta,
            log=self.log,
        )

    def send_voice(self, ogg_path: str, meta: dict[str, Any]) -> str | None:
        """Upload an OGG/Opus file and post it as a Telegram voice message."""
        if not self.ready():
            return None
        return send_voice_message(
            instance_dir=self.instance_dir,
            token=self.token,
            ogg_path=ogg_path,
            meta=meta,
        )
