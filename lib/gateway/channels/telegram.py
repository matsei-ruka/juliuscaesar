"""Telegram long-poll channel."""

from __future__ import annotations

import json
import mimetypes
import shutil
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError

from .. import chats as chats_module
from .. import queue as queue_module
from ..config import ChannelConfig, env_value
from ..format import to_markdown_v2
from ._http import http_json
from .base import EnqueueFn, LogFn


_AUDIO_MIME_EXT = {
    "audio/ogg": ".oga",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/webm": ".webm",
    "audio/x-wav": ".wav",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
}

# Backwards-compat alias — older code referenced the voice-specific name.
_VOICE_MIME_EXT = _AUDIO_MIME_EXT


def _download_telegram_file(
    token: str,
    file_id: str,
    dest: Path,
    *,
    timeout: int = 60,
) -> Path:
    """Resolve Telegram `file_id` via getFile, then stream the bytes to `dest`."""
    info = http_json(
        f"https://api.telegram.org/bot{token}/getFile?"
        + urllib.parse.urlencode({"file_id": file_id}),
        timeout=timeout,
    )
    if not info.get("ok"):
        raise RuntimeError(f"telegram getFile failed: {info}")
    file_path = (info.get("result") or {}).get("file_path")
    if not file_path:
        raise RuntimeError(f"telegram getFile missing file_path: {info}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out)
    return dest


def _transcribe_audio(audio_path: Path) -> str:
    """Best-effort ASR via `voice.asr.transcribe`. Returns "" on failure."""
    from importlib import import_module

    mod = import_module("voice.asr")
    return str(mod.transcribe(audio_path)).strip()


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

    def _main_chat_id(self) -> str | None:
        """Resolve the configured main DM chat_id.

        Auth prompts and replies fan out from the env'd `TELEGRAM_CHAT_ID`,
        which doubles as the operator's user id (DM `chat_id == user_id`).
        """
        env = env_value(self.instance_dir, "TELEGRAM_CHAT_ID")
        return str(env) if env else None

    def _is_authorized(self, chat_id: str) -> bool:
        """Decide if a chat may have its messages dispatched to the brain.

        Order:
        1. `cfg.chat_ids` (env allowlist) → always allowed (legacy contract).
        2. DM-with-the-operator → always allowed.
        3. `chats.auth_status == 'allowed'` → allowed.
        4. Otherwise → not authorized; message is dropped.
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
            self.log(f"telegram auth lookup failed chat_id={chat_id}: {exc} — fail open")
            return True
        if row is None:
            # Not yet recorded — fail open. The next `_record_chat` will
            # write the row; bot-added flow is responsible for marking
            # truly-new groups as `pending`.
            return True
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
        chat = message.get("chat") or {}
        chat_type = chat.get("type", "private")
        if chat_type in ("private", "channel"):
            return True
        if chat_type not in ("group", "supergroup"):
            return False  # unknown → fail closed
        # Reply-to-bot is unambiguous and HTTP-free: short-circuit before
        # the username check so we don't drop a legitimate reply just
        # because `getMe` hasn't resolved yet.
        reply_to = message.get("reply_to_message") or {}
        reply_from = reply_to.get("from") or {}
        if self.bot_user_id and reply_from.get("id") == self.bot_user_id:
            return True
        # 1:1 detection — if the only members are Rachel + one human, every
        # message is implicitly addressed to her. The check is HTTP-cached
        # for 5 min, so the per-message cost is one dict lookup after the
        # first call per chat.
        chat_id = str(chat.get("id", ""))
        if chat_id:
            member_count = self._get_chat_member_count(chat_id)
            if member_count is not None and member_count <= 2:
                return True
        if not self.bot_username:
            return False  # can't verify → fail closed for groups
        text = message.get("text") or message.get("caption") or ""
        entities = message.get("entities") or message.get("caption_entities") or []
        for ent in entities:
            etype = ent.get("type")
            if etype == "mention":
                offset = ent.get("offset", 0)
                length = ent.get("length", 0)
                mentioned = text[offset : offset + length].lstrip("@").lower()
                if mentioned == self.bot_username:
                    return True
            elif etype == "text_mention":
                user = ent.get("user") or {}
                if self.bot_user_id and user.get("id") == self.bot_user_id:
                    return True
        if f"@{self.bot_username}" in text.lower():
            return True
        return False

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
                    if self.allowed and chat_id not in self.allowed:
                        self.log(f"telegram ignored disallowed chat_id={chat_id}")
                        continue
                    # Run the routing decision first — it populates the
                    # member-count cache that _record_chat reads from.
                    should_process = self._should_process_message(message)
                    # Observability: record every inbound chat from an allowed
                    # source, even when we won't dispatch the message. Without
                    # this, groups Rachel is in but isn't @-mentioned in stay
                    # invisible in CHATS.md / queue.db.
                    self._record_chat(chat, message)
                    if not should_process:
                        self.log(
                            f"telegram ignored non-mention chat_id={chat_id} type={chat.get('type')}"
                        )
                        continue
                    if not self._is_authorized(chat_id):
                        self.log(
                            f"telegram ignored unauthorized chat_id={chat_id} "
                            f"type={chat.get('type')}"
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
                    enqueue(
                        source="telegram",
                        source_message_id=str(update.get("update_id")),
                        user_id=str((message.get("from") or {}).get("id", "")) or None,
                        conversation_id=conversation_id,
                        content=text,
                        meta=meta,
                    )
            except Exception as exc:  # noqa: BLE001
                self.log(f"telegram poll error: {exc}")
                time.sleep(5)
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
        if kind == "video_note":
            ext = ".mp4"
        else:
            ext = _AUDIO_MIME_EXT.get(str(payload.get("mime_type") or ""), ".oga")
        dest = self.instance_dir / "state" / "voice" / "inbound" / f"{update_id}{ext}"
        return _download_telegram_file(self.token, file_id, dest)

    def _ingest_photo(self, photos: list[Any], update_id: Any) -> Path:
        """Download the largest photo size to `state/voice/inbound/photos/`."""
        if not photos:
            raise RuntimeError("photo payload empty")
        largest = photos[-1]
        if not isinstance(largest, dict):
            raise RuntimeError("photo payload malformed")
        file_id = largest.get("file_id")
        if not file_id:
            raise RuntimeError("photo payload missing file_id")
        dest = (
            self.instance_dir / "state" / "voice" / "inbound" / "photos" / f"{update_id}.jpg"
        )
        return _download_telegram_file(self.token, file_id, dest)

    def _ingest_document(self, document: dict[str, Any], update_id: Any) -> Path:
        """Download a Telegram document to `state/voice/inbound/docs/`.

        Preserves the original file extension when present; falls back to a
        MIME-derived extension; finally `.bin` if neither is available.
        """
        file_id = document.get("file_id")
        if not file_id:
            raise RuntimeError("document payload missing file_id")
        original = document.get("file_name") or ""
        ext = Path(original).suffix
        if not ext:
            mime = str(document.get("mime_type") or "")
            ext = mimetypes.guess_extension(mime) or ".bin"
        dest = (
            self.instance_dir / "state" / "voice" / "inbound" / "docs" / f"{update_id}{ext}"
        )
        return _download_telegram_file(self.token, file_id, dest)

    def _dm_title(self, chat: dict[str, Any]) -> str | None:
        """Compose a DM display name as `first_name + last_name` per spec.

        Falls back to `@username` when neither name field is set.
        """
        first = (chat.get("first_name") or "").strip()
        last = (chat.get("last_name") or "").strip()
        full = " ".join(part for part in (first, last) if part)
        if full:
            return full
        username = chat.get("username")
        if username:
            return f"@{username}"
        return None

    def _cached_member_count(self, chat_id: str) -> int | None:
        """Return a cached `getChatMemberCount` if present — never fetch.

        Chat-recording is observability; it must not trigger new HTTP calls
        on top of the message-handling path. If the value is already in the
        cache (typically populated by `_get_chat_member_count` during the
        `_should_process_message` check), use it; otherwise return None and
        let the row's `member_count` stay NULL until the next inbound
        message refreshes it.
        """
        entry = self._member_count_cache.get(chat_id)
        return entry[0] if entry else None

    def _record_chat(self, chat: dict[str, Any], message: dict[str, Any]) -> None:
        """Upsert the inbound chat into the chats directory.

        Wrapped in try/except — chat tracking must never block message
        processing. Failures are logged once per occurrence. Reuses the
        channel's cached SQLite connection to avoid per-message
        connection churn.
        """
        try:
            chat_id = str(chat.get("id", ""))
            if not chat_id:
                return
            chat_type = chat.get("type")
            if chat_type in ("group", "supergroup", "channel"):
                title = chat.get("title") or self._dm_title(chat)
            else:
                title = self._dm_title(chat) or chat.get("title")
            chats_module.upsert_chat(
                instance_dir=self.instance_dir,
                conn=self._get_chats_conn(),
                channel="telegram",
                chat_id=chat_id,
                chat_type=chat_type,
                title=title,
                username=chat.get("username"),
                member_count=self._cached_member_count(chat_id),
                last_message_id=(
                    str(message.get("message_id"))
                    if message.get("message_id") is not None
                    else None
                ),
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"telegram chat upsert failed chat_id={chat.get('id')}: {exc}")

    def _log_forward(self, message: dict[str, Any], update_id: Any) -> None:
        """If the message is a forward, log a single audit line."""
        ff = message.get("forward_from") or message.get("forward_from_chat")
        if not isinstance(ff, dict):
            return
        ident = ff.get("username") or ff.get("title") or ff.get("id") or "-"
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
        payload: dict[str, Any] = {"chat_id": str(chat_id), "action": "typing"}
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id
        http_json(
            f"https://api.telegram.org/bot{self.token}/sendChatAction",
            data=payload,
            timeout=10,
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
        chat_id = str(
            meta.get("chat_id")
            or meta.get("notify_chat_id")
            or env_value(self.instance_dir, "TELEGRAM_CHAT_ID")
            or ""
        )
        if not chat_id:
            return None
        original = response[:4096]
        escaped = to_markdown_v2(original)
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": escaped,
            "disable_web_page_preview": True,
            "parse_mode": "MarkdownV2",
        }
        if meta.get("message_thread_id"):
            payload["message_thread_id"] = meta["message_thread_id"]
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            data = http_json(url, data=payload, timeout=15)
            parse_error = (
                not data.get("ok")
                and "parse" in str(data.get("description") or "").lower()
            )
            error_desc = str(data.get("description") or "")
        except HTTPError as exc:
            # Telegram returns 400 with a JSON body describing the parse
            # failure. urllib.urlopen raises HTTPError before we get to read.
            try:
                body_text = exc.read().decode("utf-8", errors="replace")
                body = json.loads(body_text) if body_text else {}
            except (json.JSONDecodeError, ValueError):
                body = {}
            error_desc = str(body.get("description") or exc.reason or "")
            parse_error = exc.code == 400 and (
                "parse" in error_desc.lower() or "entit" in error_desc.lower()
            )
            data = body if isinstance(body, dict) else {}
            if not parse_error:
                raise RuntimeError(f"telegram send failed: HTTP {exc.code} {error_desc}") from exc
        if parse_error:
            self.log(
                f"telegram.send.parse_error retrying without parse_mode err={error_desc!r}"
            )
            fallback: dict[str, Any] = {
                "chat_id": chat_id,
                "text": original,
                "disable_web_page_preview": True,
            }
            if meta.get("message_thread_id"):
                fallback["message_thread_id"] = meta["message_thread_id"]
            data = http_json(url, data=fallback, timeout=15)
        if not data.get("ok"):
            raise RuntimeError(f"telegram send failed: {data}")
        result = data.get("result") or {}
        return str(result.get("message_id")) if result.get("message_id") is not None else None

    def send_voice(self, ogg_path: str, meta: dict[str, Any]) -> str | None:
        """Upload an OGG/Opus file and post it as a Telegram voice message."""
        if not self.ready():
            return None
        chat_id = str(
            meta.get("chat_id")
            or meta.get("notify_chat_id")
            or env_value(self.instance_dir, "TELEGRAM_CHAT_ID")
            or ""
        )
        if not chat_id:
            return None
        path = Path(ogg_path)
        if not path.exists():
            raise RuntimeError(f"telegram sendVoice missing file: {ogg_path}")

        fields: list[tuple[str, str]] = [("chat_id", chat_id)]
        if meta.get("message_thread_id"):
            fields.append(("message_thread_id", str(meta["message_thread_id"])))
        files: list[tuple[str, str, bytes, str]] = [
            ("voice", path.name, path.read_bytes(), "audio/ogg"),
        ]
        body, content_type = _encode_multipart(fields, files)
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendVoice",
            data=body,
            headers={"Content-Type": content_type},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw else {}
        if not data.get("ok"):
            raise RuntimeError(f"telegram sendVoice failed: {data}")
        result = data.get("result") or {}
        return str(result.get("message_id")) if result.get("message_id") is not None else None


def _encode_multipart(
    fields: list[tuple[str, str]],
    files: list[tuple[str, str, bytes, str]],
) -> tuple[bytes, str]:
    """Build a `multipart/form-data` body for urllib.

    `fields` is a list of (name, value) string pairs.
    `files` is a list of (name, filename, data, content_type) tuples.
    Returns (body_bytes, content_type_header).
    """
    boundary = f"----jcboundary{uuid.uuid4().hex}"
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields:
        parts.append(f"--{boundary}".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{name}"'.encode("utf-8"))
        parts.append(b"")
        parts.append(value.encode("utf-8"))
    for name, filename, data, content_type in files:
        parts.append(f"--{boundary}".encode("utf-8"))
        parts.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode(
                "utf-8"
            )
        )
        parts.append(f"Content-Type: {content_type or mimetypes.guess_type(filename)[0] or 'application/octet-stream'}".encode("utf-8"))
        parts.append(b"")
        parts.append(data)
    parts.append(f"--{boundary}--".encode("utf-8"))
    parts.append(b"")
    body = crlf.join(parts)
    return body, f"multipart/form-data; boundary={boundary}"
