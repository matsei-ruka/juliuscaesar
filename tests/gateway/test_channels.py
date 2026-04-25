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

from gateway.channels import telegram as telegram_module  # noqa: E402
from gateway.channels.cron import CronChannel  # noqa: E402
from gateway.channels.jc_events import JcEventsChannel  # noqa: E402
from gateway.channels.telegram import TelegramChannel  # noqa: E402
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


class TelegramVoiceIngestionTests(unittest.TestCase):
    """Regression: Telegram voice messages must be transcribed and enqueued."""

    def _drive_channel(self, instance: Path, updates, *, transcript: str):
        served = {"done": False}

        def fake_http_json(url, *, data=None, timeout=15, **_):
            if "getUpdates" in url:
                if served["done"]:
                    return {"ok": True, "result": []}
                served["done"] = True
                return {"ok": True, "result": updates}
            if "getFile" in url:
                return {"ok": True, "result": {"file_path": "voice/file_1.oga"}}
            return {"ok": True, "result": {}}

        class FakeResp:
            def __init__(self, payload: bytes):
                self._buf = payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n=-1):
                if n is None or n < 0:
                    out, self._buf = self._buf, b""
                    return out
                out, self._buf = self._buf[:n], self._buf[n:]
                return out

        captured: list[dict] = []

        def enqueue(**kwargs):
            captured.append(kwargs)

        stop_after = {"done": False}

        def should_stop() -> bool:
            return stop_after["done"]

        cfg = ChannelConfig(enabled=True, token_env="TELEGRAM_BOT_TOKEN", chat_ids=["28547271"])
        channel = TelegramChannel(instance, cfg, _silent_log)
        channel.token = "test-token"  # bypass env_value lookup

        orig_http_json = telegram_module.http_json
        orig_urlopen = telegram_module.urllib.request.urlopen
        orig_transcribe = telegram_module._transcribe_audio
        telegram_module.http_json = fake_http_json
        telegram_module.urllib.request.urlopen = lambda *_a, **_k: FakeResp(b"OggS\x00\x00")
        telegram_module._transcribe_audio = lambda _path: transcript
        try:
            thread = threading.Thread(
                target=channel.run, args=(enqueue, should_stop), daemon=True
            )
            thread.start()
            for _ in range(20):
                if served["done"] and captured:
                    break
                time.sleep(0.1)
            stop_after["done"] = True
            thread.join(timeout=3)
        finally:
            telegram_module.http_json = orig_http_json
            telegram_module.urllib.request.urlopen = orig_urlopen
            telegram_module._transcribe_audio = orig_transcribe
        return captured

    def test_voice_message_is_transcribed_and_enqueued(self):
        update = {
            "update_id": 99,
            "message": {
                "message_id": 17,
                "chat": {"id": 28547271},
                "from": {"id": 28547271, "username": "luca"},
                "voice": {"file_id": "AwACA-voice", "mime_type": "audio/ogg", "duration": 3},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            captured = self._drive_channel(instance, [update], transcript="check the params")
            self.assertEqual(len(captured), 1, "voice update should produce exactly one enqueue")
            kwargs = captured[0]
            self.assertEqual(kwargs["source"], "telegram")
            self.assertEqual(kwargs["content"], "check the params")
            self.assertTrue(kwargs["meta"]["was_voice"])
            self.assertIn("audio_path", kwargs["meta"])
            audio_path = Path(kwargs["meta"]["audio_path"])
            self.assertTrue(audio_path.exists())
            self.assertTrue(
                audio_path.is_relative_to(instance / "state" / "voice" / "inbound")
            )

    def test_text_message_unchanged(self):
        update = {
            "update_id": 100,
            "message": {
                "message_id": 18,
                "chat": {"id": 28547271},
                "from": {"id": 28547271, "username": "luca"},
                "text": "plain text",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            captured = self._drive_channel(instance, [update], transcript="unused")
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["content"], "plain text")
            self.assertNotIn("was_voice", captured[0]["meta"])
            self.assertNotIn("audio_path", captured[0]["meta"])


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
