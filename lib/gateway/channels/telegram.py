"""Telegram long-poll channel."""

from __future__ import annotations

import shutil
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..config import ChannelConfig, env_value
from ._http import http_json
from .base import EnqueueFn, LogFn


_VOICE_MIME_EXT = {
    "audio/ogg": ".oga",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-wav": ".wav",
    "audio/wav": ".wav",
}


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
                    text = message.get("text") or message.get("caption") or ""
                    voice = message.get("voice") if isinstance(message.get("voice"), dict) else None
                    audio_path: Path | None = None
                    if not text.strip() and voice:
                        update_id = update.get("update_id")
                        try:
                            audio_path = self._ingest_voice(voice, update_id)
                            text = _transcribe_audio(audio_path)
                        except Exception as exc:  # noqa: BLE001
                            self.log(
                                f"telegram voice ingestion failed update_id={update_id}: {exc}"
                            )
                            continue
                        if not text.strip():
                            self.log(
                                f"telegram voice transcription empty update_id={update_id}"
                            )
                            continue
                        self.log(
                            f"telegram voice transcribed update_id={update_id} chars={len(text)}"
                        )
                    if not text.strip():
                        continue
                    thread_id = message.get("message_thread_id")
                    conversation_id = f"{chat_id}:{thread_id}" if thread_id else chat_id
                    meta: dict[str, Any] = {
                        "chat_id": chat_id,
                        "message_id": message.get("message_id"),
                        "message_thread_id": thread_id,
                        "username": (message.get("from") or {}).get("username"),
                    }
                    if audio_path is not None:
                        meta["was_voice"] = True
                        meta["audio_path"] = str(audio_path)
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

    def _ingest_voice(self, voice: dict[str, Any], update_id: Any) -> Path:
        """Download a Telegram voice attachment to `state/voice/inbound/`."""
        file_id = voice.get("file_id")
        if not file_id:
            raise RuntimeError("voice payload missing file_id")
        ext = _VOICE_MIME_EXT.get(str(voice.get("mime_type") or ""), ".oga")
        dest = self.instance_dir / "state" / "voice" / "inbound" / f"{update_id}{ext}"
        return _download_telegram_file(self.token, file_id, dest)

    def send(self, response: str, meta: dict[str, Any]) -> str | None:
        if not self.ready() or not response.strip():
            return None
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
