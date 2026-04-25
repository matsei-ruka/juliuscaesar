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

from ..config import ChannelConfig, env_value
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

    def __init__(self, instance_dir: Path, cfg: ChannelConfig, log: LogFn):
        self.instance_dir = instance_dir
        self.cfg = cfg
        self.log = log
        self.token = env_value(instance_dir, cfg.token_env)
        self.offset = 0
        self.allowed = set(cfg.chat_ids)

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
                    {"timeout": self.cfg.timeout_seconds, "offset": self.offset}
                )
                data = http_json(f"{url}?{params}", timeout=self.cfg.timeout_seconds + 5)
                for update in data.get("result", []):
                    self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
                    message = update.get("message") or update.get("edited_message")
                    if not isinstance(message, dict):
                        continue
                    chat = message.get("chat") or {}
                    chat_id = str(chat.get("id", ""))
                    if self.allowed and chat_id not in self.allowed:
                        self.log(f"telegram ignored disallowed chat_id={chat_id}")
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
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": response[:4096],
            "disable_web_page_preview": True,
        }
        if meta.get("message_thread_id"):
            payload["message_thread_id"] = meta["message_thread_id"]
        data = http_json(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=payload,
            timeout=15,
        )
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
