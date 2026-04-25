"""Gateway runtime loop: channels, dispatcher, delivery."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import queue
from .brain import call_brain
from .channels import SlackSocketModeChannel, TelegramChannel, deliver
from .config import GatewayConfig, load_config


def decode_meta(event: queue.Event) -> dict[str, Any]:
    if not event.meta:
        return {}
    try:
        data = json.loads(event.meta)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


class GatewayRuntime:
    def __init__(
        self,
        instance_dir: Path,
        *,
        log_path: Path,
        stop_requested: Callable[[], bool],
    ):
        self.instance_dir = instance_dir
        self.config = load_config(instance_dir)
        self.log_path = log_path
        self.stop_requested = stop_requested
        self.worker_id = f"gateway-{os.getpid()}"
        self.threads: list[threading.Thread] = []

    def log(self, message: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{queue.now_iso()}] {message}\n")

    def enqueue(self, **kwargs: Any) -> None:
        conn = queue.connect(self.instance_dir)
        try:
            event, inserted = queue.enqueue(conn, **kwargs)
        finally:
            conn.close()
        self.log(
            f"event {'enqueued' if inserted else 'deduped'} id={event.id} "
            f"source={event.source} conversation={event.conversation_id or '-'}"
        )

    def start_channels(self) -> None:
        channels = []
        if self.config.channel("telegram").enabled:
            channels.append(TelegramChannel(self.instance_dir, self.config.channel("telegram"), self.log))
        if self.config.channel("slack").enabled:
            channels.append(SlackSocketModeChannel(self.instance_dir, self.config.channel("slack"), self.log))
        for channel in channels:
            thread = threading.Thread(
                target=channel.run,
                args=(self.enqueue, self.stop_requested),
                name=f"gateway-{channel.name}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)

    def run_forever(self) -> None:
        self.start_channels()
        self.log("dispatcher started")
        while not self.stop_requested():
            self.dispatch_once()
            time.sleep(self.config.poll_interval_seconds)
        self.log("dispatcher stopping")
        for thread in self.threads:
            thread.join(timeout=2)

    def dispatch_once(self) -> bool:
        conn = queue.connect(self.instance_dir)
        try:
            event = queue.claim_next(
                conn,
                worker_id=self.worker_id,
                lease_seconds=self.config.lease_seconds,
            )
        finally:
            conn.close()
        if event is None:
            return False
        try:
            response = self.process_event(event)
            conn2 = queue.connect(self.instance_dir)
            try:
                queue.complete(conn2, event.id, response=response)
            finally:
                conn2.close()
            self.log(f"event done id={event.id} source={event.source}")
        except Exception as exc:  # noqa: BLE001
            conn3 = queue.connect(self.instance_dir)
            try:
                failed = queue.fail(
                    conn3,
                    event.id,
                    error=str(exc)[:1000],
                    max_retries=self.config.max_retries,
                )
            finally:
                conn3.close()
            self.log(f"event {failed.status} id={event.id} error={exc}")
        return True

    def process_event(self, event: queue.Event) -> str:
        meta = decode_meta(event)
        if meta.get("deliver_only"):
            response = event.content
            deliver(
                instance_dir=self.instance_dir,
                source=event.source,
                response=response,
                meta=meta,
                config_channels=self.config.channels,
                log=self.log,
            )
            return response

        channel = event.source if event.source in ("telegram", "slack") else str(meta.get("channel") or event.source)
        brain, model = self.config.brain_for(channel)
        resume_session = None
        if event.conversation_id:
            conn = queue.connect(self.instance_dir)
            try:
                existing = queue.get_session(
                    conn,
                    channel=channel,
                    conversation_id=event.conversation_id,
                    brain=brain,
                )
                resume_session = existing.session_id if existing else None
            finally:
                conn.close()
        result = call_brain(
            instance_dir=self.instance_dir,
            event=event,
            brain=brain,
            model=model,
            resume_session=resume_session,
            timeout_seconds=self.config.adapter_timeout_seconds,
            log_path=self.log_path,
        )
        if result.session_id and event.conversation_id:
            conn2 = queue.connect(self.instance_dir)
            try:
                queue.upsert_session(
                    conn2,
                    channel=channel,
                    conversation_id=event.conversation_id,
                    brain=brain,
                    session_id=result.session_id,
                )
            finally:
                conn2.close()
        response = result.response or "(no response)"
        meta.setdefault("delivery_channel", channel)
        deliver(
            instance_dir=self.instance_dir,
            source=channel,
            response=response,
            meta=meta,
            config_channels=self.config.channels,
            log=self.log,
        )
        return response
