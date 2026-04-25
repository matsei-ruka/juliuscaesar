"""Telegram long-poll channel."""

from __future__ import annotations

import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from ..config import ChannelConfig, env_value
from ._http import http_json
from .base import EnqueueFn, LogFn


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
                    if not text.strip():
                        continue
                    thread_id = message.get("message_thread_id")
                    conversation_id = f"{chat_id}:{thread_id}" if thread_id else chat_id
                    enqueue(
                        source="telegram",
                        source_message_id=str(update.get("update_id")),
                        user_id=str((message.get("from") or {}).get("id", "")) or None,
                        conversation_id=conversation_id,
                        content=text,
                        meta={
                            "chat_id": chat_id,
                            "message_id": message.get("message_id"),
                            "message_thread_id": thread_id,
                            "username": (message.get("from") or {}).get("username"),
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                self.log(f"telegram poll error: {exc}")
                time.sleep(5)
        self.log("telegram poller stopped")

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
