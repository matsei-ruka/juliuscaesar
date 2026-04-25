"""Internal `jc-events` channel.

Watches `<instance>/state/events/*.json` and enqueues a synthesis prompt for
worker / system events. Uses a poll loop (1–2s by default) so it works on
macOS without inotify and on Linux without extra deps.

Each event file is consumed (deleted) once enqueued. Files that fail to parse
are renamed `<file>.bad` so they do not loop forever.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from ..config import ChannelConfig
from .base import EnqueueFn, LogFn


class JcEventsChannel:
    name = "jc-events"

    def __init__(self, instance_dir: Path, cfg: ChannelConfig, log: LogFn):
        self.instance_dir = instance_dir
        self.cfg = cfg
        self.log = log
        watch_dir = getattr(cfg, "watch_dir", "state/events") or "state/events"
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
            self.log(f"jc-events disabled: cannot create {self.watch_dir}")
            return
        self.log(f"jc-events watching {self.watch_dir}")
        while not should_stop():
            try:
                for path in sorted(self.watch_dir.glob("*.json")):
                    self._process_file(path, enqueue)
            except Exception as exc:  # noqa: BLE001
                self.log(f"jc-events error: {exc}")
            time.sleep(self.poll_interval)
        self.log("jc-events stopped")

    def _process_file(self, path: Path, enqueue: EnqueueFn) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self.log(f"jc-events bad file {path.name}: {exc}")
            try:
                path.rename(path.with_suffix(path.suffix + ".bad"))
            except OSError:
                path.unlink(missing_ok=True)
            return

        if not isinstance(payload, dict):
            self.log(f"jc-events skip non-object {path.name}")
            path.unlink(missing_ok=True)
            return

        event_type = str(payload.get("event_type") or "system.event")
        content = self._render_content(event_type, payload)
        meta: dict[str, Any] = {
            "event_type": event_type,
            "payload": payload,
            "delivery_channel": str(payload.get("notify_channel") or "telegram"),
        }
        notify_chat_id = payload.get("notify_chat_id")
        if notify_chat_id:
            meta["chat_id"] = str(notify_chat_id)
            meta["notify_chat_id"] = str(notify_chat_id)
        notify_thread_ts = payload.get("notify_thread_ts")
        if notify_thread_ts:
            meta["thread_ts"] = str(notify_thread_ts)
            meta["channel"] = payload.get("notify_slack_channel")

        enqueue(
            source="jc-events",
            source_message_id=str(payload.get("event_id") or path.stem),
            user_id=str(payload.get("notify_chat_id") or "") or None,
            conversation_id=str(payload.get("conversation_id") or "") or None,
            content=content,
            meta=meta,
        )
        path.unlink(missing_ok=True)

    def _render_content(self, event_type: str, payload: dict) -> str:
        if event_type == "worker.completed":
            wid = payload.get("worker_id")
            topic = payload.get("topic") or ""
            status = payload.get("status") or "done"
            duration = payload.get("duration_seconds")
            result = payload.get("result_path")
            duration_text = f" ({int(duration)}s)" if isinstance(duration, (int, float)) else ""
            result_text = f"\n\nResult file: {result}" if result else ""
            return (
                f"Background worker #{wid} '{topic}' completed [{status}]{duration_text}.\n"
                "Synthesize a short, friendly summary for the user — focus on the outcome, "
                "the headline number, and any next step. Read the result file if useful."
                f"{result_text}"
            )
        return (
            f"System event of type '{event_type}'. Payload:\n```json\n"
            f"{json.dumps(payload, indent=2, sort_keys=True)}\n```\n"
            "Summarize for the user only if it is useful; otherwise reply with an empty string."
        )

    def send(self, response: str, meta: dict[str, Any]) -> str | None:
        return None
