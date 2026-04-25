"""Gateway channel clients for Telegram and Slack Socket Mode."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import queue
from .config import ChannelConfig, env_value


EnqueueFn = Callable[..., None]
LogFn = Callable[[str], None]


@dataclass(frozen=True)
class DeliveryTarget:
    channel: str
    conversation_id: str | None
    meta: dict[str, Any]


def http_json(
    url: str,
    *,
    token: str | None = None,
    data: dict[str, Any] | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    body = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw else {}


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


class SlackSocketModeChannel:
    name = "slack"

    def __init__(self, instance_dir: Path, cfg: ChannelConfig, log: LogFn):
        self.instance_dir = instance_dir
        self.cfg = cfg
        self.log = log
        self.app_token = env_value(instance_dir, cfg.app_token_env)
        self.bot_token = env_value(instance_dir, cfg.bot_token_env)

    def ready(self) -> bool:
        return bool(self.app_token and self.bot_token)

    def run(self, enqueue: EnqueueFn, should_stop: Callable[[], bool]) -> None:
        if not self.ready():
            self.log("slack disabled: app or bot token missing")
            return
        try:
            import websocket  # type: ignore
        except Exception:
            self.log("slack disabled: install websocket-client for Socket Mode")
            return
        self.log("slack socket-mode poller started")
        while not should_stop():
            ws = None
            try:
                opened = http_json(
                    "https://slack.com/api/apps.connections.open",
                    token=self.app_token,
                    data={},
                )
                if not opened.get("ok"):
                    raise RuntimeError(opened.get("error") or opened)
                ws = websocket.create_connection(opened["url"], timeout=30)
                while not should_stop():
                    raw = ws.recv()
                    envelope = json.loads(raw)
                    envelope_id = envelope.get("envelope_id")
                    if envelope_id:
                        ws.send(json.dumps({"envelope_id": envelope_id}))
                    payload = envelope.get("payload") or {}
                    event = payload.get("event") or {}
                    if event.get("type") != "message" or event.get("bot_id"):
                        continue
                    text = event.get("text") or ""
                    if not text.strip():
                        continue
                    channel = str(event.get("channel") or "")
                    ts = str(event.get("ts") or "")
                    thread_ts = str(event.get("thread_ts") or ts)
                    conversation_id = f"{channel}:{thread_ts}"
                    enqueue(
                        source="slack",
                        source_message_id=str(payload.get("event_id") or ts),
                        user_id=str(event.get("user") or ""),
                        conversation_id=conversation_id,
                        content=text,
                        meta={
                            "channel": channel,
                            "ts": ts,
                            "thread_ts": thread_ts,
                            "event_id": payload.get("event_id"),
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                self.log(f"slack socket error: {exc}")
                time.sleep(5)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
        self.log("slack socket-mode poller stopped")

    def send(self, response: str, meta: dict[str, Any]) -> str | None:
        if not self.ready() or not response.strip():
            return None
        channel = str(meta.get("channel") or "")
        if not channel:
            return None
        payload = {
            "channel": channel,
            "text": response[:40000],
            "thread_ts": meta.get("thread_ts") or meta.get("ts"),
        }
        data = http_json(
            "https://slack.com/api/chat.postMessage",
            token=self.bot_token,
            data=payload,
            timeout=15,
        )
        if not data.get("ok"):
            raise RuntimeError(f"slack send failed: {data}")
        return str(data.get("ts")) if data.get("ts") is not None else None


def deliver(
    *,
    instance_dir: Path,
    source: str,
    response: str,
    meta: dict[str, Any],
    config_channels: dict[str, ChannelConfig],
    log: LogFn,
) -> str | None:
    channel = meta.get("delivery_channel") or source
    if channel == "telegram":
        return TelegramChannel(instance_dir, config_channels["telegram"], log).send(response, meta)
    if channel == "slack":
        return SlackSocketModeChannel(instance_dir, config_channels["slack"], log).send(response, meta)
    log(f"delivery skipped for source={source}")
    return None
