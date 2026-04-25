"""Cron channel — bridges scheduled heartbeat tasks into the gateway queue.

Without this channel, `lib/heartbeat/runner.py` invokes adapters directly and
delivery uses `lib/heartbeat/lib/send_telegram.sh`. With this channel enabled,
heartbeat hash-deltas enqueue an event with `meta.brain_override` so the
selected brain runs through the gateway runtime and routing/sticky/triage all
apply. The cron channel is inbound-only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from ..config import ChannelConfig
from .base import EnqueueFn, LogFn


class CronChannel:
    name = "cron"

    def __init__(self, instance_dir: Path, cfg: ChannelConfig, log: LogFn):
        self.instance_dir = instance_dir
        self.cfg = cfg
        self.log = log
        watch_dir = getattr(cfg, "watch_dir", "state/cron") or "state/cron"
        self.watch_dir = (instance_dir / watch_dir).resolve()
        self.poll_interval = max(1, int(getattr(cfg, "poll_interval_seconds", 2) or 2))

    def ready(self) -> bool:
        try:
            self.watch_dir.mkdir(parents=True, exist_ok=True)
            return True
        except OSError:
            return False

    def run(self, enqueue: EnqueueFn, should_stop: Callable[[], bool]) -> None:
        if not self.ready():
            self.log("cron disabled: cannot create watch dir")
            return
        self.log(f"cron channel watching {self.watch_dir}")
        while not should_stop():
            try:
                for path in sorted(self.watch_dir.glob("*.json")):
                    self._process_file(path, enqueue)
            except Exception as exc:  # noqa: BLE001
                self.log(f"cron error: {exc}")
            time.sleep(self.poll_interval)
        self.log("cron channel stopped")

    def _process_file(self, path: Path, enqueue: EnqueueFn) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self.log(f"cron bad file {path.name}: {exc}")
            try:
                path.rename(path.with_suffix(path.suffix + ".bad"))
            except OSError:
                path.unlink(missing_ok=True)
            return

        if not isinstance(payload, dict):
            path.unlink(missing_ok=True)
            return

        task_name = str(payload.get("task_name") or path.stem)
        prompt = str(payload.get("prompt") or "")
        brain = payload.get("brain")
        model = payload.get("model")
        notify_channel = str(payload.get("notify_channel") or "telegram")
        notify_chat_id = payload.get("notify_chat_id")

        meta: dict[str, Any] = {
            "task_name": task_name,
            "delivery_channel": notify_channel,
        }
        if brain:
            spec = str(brain) if not model else f"{brain}:{model}"
            meta["brain"] = spec
        if model:
            meta["model"] = str(model)
        if notify_chat_id:
            meta["chat_id"] = str(notify_chat_id)
        if payload.get("notify_thread_ts"):
            meta["thread_ts"] = str(payload["notify_thread_ts"])
            meta["channel"] = payload.get("notify_slack_channel")

        enqueue(
            source="cron",
            source_message_id=str(payload.get("run_id") or path.stem),
            user_id=str(payload.get("notify_chat_id") or "") or None,
            conversation_id=f"cron:{task_name}",
            content=prompt or f"Run scheduled task '{task_name}'.",
            meta=meta,
        )
        path.unlink(missing_ok=True)

    def send(self, response: str, meta: dict[str, Any]) -> str | None:
        return None
