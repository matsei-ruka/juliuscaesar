"""Tests for the new file-watching channels (jc-events, cron)."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.channels.cron import CronChannel  # noqa: E402
from gateway.channels.jc_events import JcEventsChannel  # noqa: E402
from gateway.config import ChannelConfig, ConfigError, load_config, render_default_config  # noqa: E402


def _silent_log(message: str) -> None:  # noqa: ARG001
    pass


def _run_channel_once(channel) -> list[dict]:
    captured: list[dict] = []

    def enqueue(**kwargs):
        captured.append(kwargs)

    stop_after = {"done": False}

    def should_stop() -> bool:
        return stop_after["done"]

    thread = threading.Thread(target=channel.run, args=(enqueue, should_stop), daemon=True)
    thread.start()
    # Channels poll once per second by default; we configured 1s in the tests.
    time.sleep(1.5)
    stop_after["done"] = True
    thread.join(timeout=3)
    return captured


class JcEventsChannelTests(unittest.TestCase):
    def test_worker_completion_renders_synthesis_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            events_dir = instance / "state" / "events"
            events_dir.mkdir(parents=True)
            (events_dir / "worker-18.json").write_text(
                json.dumps(
                    {
                        "event_type": "worker.completed",
                        "event_id": "worker-18-done",
                        "worker_id": 18,
                        "topic": "fix bugs",
                        "status": "done",
                        "duration_seconds": 145,
                        "result_path": "state/workers/18/result",
                        "notify_channel": "telegram",
                        "notify_chat_id": "28547271",
                    }
                ),
                encoding="utf-8",
            )

            cfg = ChannelConfig(enabled=True, watch_dir="state/events", poll_interval_seconds=1)
            channel = JcEventsChannel(instance, cfg, _silent_log)
            captured = _run_channel_once(channel)

            self.assertEqual(len(captured), 1)
            kwargs = captured[0]
            self.assertEqual(kwargs["source"], "jc-events")
            self.assertIn("worker #18", kwargs["content"])
            self.assertIn("fix bugs", kwargs["content"])
            self.assertEqual(kwargs["meta"]["delivery_channel"], "telegram")
            self.assertEqual(kwargs["meta"]["chat_id"], "28547271")
            # File consumed.
            self.assertEqual(list((instance / "state" / "events").glob("*.json")), [])

    def test_bad_json_renamed_with_bad_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            events_dir = instance / "state" / "events"
            events_dir.mkdir(parents=True)
            (events_dir / "broken.json").write_text("{not json", encoding="utf-8")

            cfg = ChannelConfig(enabled=True, watch_dir="state/events", poll_interval_seconds=1)
            channel = JcEventsChannel(instance, cfg, _silent_log)
            _run_channel_once(channel)

            files = list(events_dir.iterdir())
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].name.endswith(".bad"))


class CronChannelTests(unittest.TestCase):
    def test_pinned_brain_propagates_to_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            cron_dir = instance / "state" / "cron"
            cron_dir.mkdir(parents=True)
            (cron_dir / "morning.json").write_text(
                json.dumps(
                    {
                        "task_name": "morning_briefing",
                        "prompt": "Summarize the news.",
                        "brain": "claude",
                        "model": "opus-4-7-1m",
                        "notify_channel": "telegram",
                        "notify_chat_id": "12345",
                        "run_id": "morning-2026-04-25",
                    }
                ),
                encoding="utf-8",
            )

            cfg = ChannelConfig(enabled=True, watch_dir="state/cron", poll_interval_seconds=1)
            channel = CronChannel(instance, cfg, _silent_log)
            captured = _run_channel_once(channel)

            self.assertEqual(len(captured), 1)
            kwargs = captured[0]
            self.assertEqual(kwargs["source"], "cron")
            self.assertEqual(kwargs["conversation_id"], "cron:morning_briefing")
            self.assertEqual(kwargs["meta"]["brain"], "claude:opus-4-7-1m")
            self.assertEqual(kwargs["meta"]["model"], "opus-4-7-1m")
            self.assertEqual(kwargs["meta"]["delivery_channel"], "telegram")


class ConfigWebRejectionTests(unittest.TestCase):
    def test_web_channel_rejected_with_helpful_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".jc").write_text("", encoding="utf-8")
            (instance / "ops").mkdir()
            base = render_default_config(default_brain="claude")
            (instance / "ops" / "gateway.yaml").write_text(
                base + "  web:\n    enabled: true\n    port: 8787\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("web channel removed", str(ctx.exception))

    def test_default_config_still_loads_with_new_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".jc").write_text("", encoding="utf-8")
            (instance / "ops").mkdir()
            (instance / "ops" / "gateway.yaml").write_text(
                render_default_config(default_brain="claude", triage_backend="openrouter"),
                encoding="utf-8",
            )
            cfg = load_config(instance)
            self.assertEqual(cfg.triage.backend, "openrouter")
            self.assertTrue(cfg.channel("jc-events").enabled)
            self.assertTrue(cfg.channel("cron").enabled)
            self.assertFalse(cfg.channel("discord").enabled)
            self.assertFalse(cfg.channel("voice").enabled)


if __name__ == "__main__":
    unittest.main()
