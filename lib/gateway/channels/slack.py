"""Slack Socket Mode channel."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from ..config import ChannelConfig, env_value
from ._http import http_json
from .base import EnqueueFn, LogFn


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
