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

from gateway import runtime as runtime_module  # noqa: E402
from gateway.channels import telegram as telegram_module  # noqa: E402
from gateway.channels.cron import CronChannel  # noqa: E402
from gateway.channels.jc_events import JcEventsChannel  # noqa: E402
from gateway.channels.telegram import TelegramChannel  # noqa: E402
from gateway.channels.voice import VoiceChannel  # noqa: E402
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


def _drive_telegram(
    instance: Path,
    updates,
    *,
    transcript: str | None = None,
    urlopen_payload: bytes = b"FAKE",
    log=None,
    expect_capture: bool = True,
):
    """Drive `TelegramChannel.run` through one fake getUpdates batch.

    Returns the list of `enqueue(**kwargs)` calls. Mocks `http_json`,
    `urlopen`, and (when `transcript` is provided) `_transcribe_audio`.
    """
    served = {"done": False}

    def fake_http_json(url, *, data=None, timeout=15, **_):
        if "getUpdates" in url:
            if served["done"]:
                return {"ok": True, "result": []}
            served["done"] = True
            return {"ok": True, "result": updates}
        if "getFile" in url:
            return {"ok": True, "result": {"file_path": "x/file"}}
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
    channel = TelegramChannel(instance, cfg, log or _silent_log)
    channel.token = "test-token"

    orig_http_json = telegram_module.http_json
    orig_urlopen = telegram_module.urllib.request.urlopen
    orig_transcribe = telegram_module._transcribe_audio
    telegram_module.http_json = fake_http_json
    telegram_module.urllib.request.urlopen = lambda *_a, **_k: FakeResp(urlopen_payload)
    if transcript is not None:
        telegram_module._transcribe_audio = lambda _path: transcript
    try:
        thread = threading.Thread(
            target=channel.run, args=(enqueue, should_stop), daemon=True
        )
        thread.start()
        for _ in range(40):
            if served["done"] and (not expect_capture or captured):
                break
            time.sleep(0.1)
        stop_after["done"] = True
        thread.join(timeout=3)
    finally:
        telegram_module.http_json = orig_http_json
        telegram_module.urllib.request.urlopen = orig_urlopen
        if transcript is not None:
            telegram_module._transcribe_audio = orig_transcribe
    return captured


class TelegramVoiceIngestionTests(unittest.TestCase):
    """Regression: Telegram voice messages must be transcribed and enqueued."""

    def _drive_channel(self, instance: Path, updates, *, transcript: str):
        return _drive_telegram(instance, updates, transcript=transcript, urlopen_payload=b"OggS\x00\x00")

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


class TelegramAudioVideoIngestionTests(unittest.TestCase):
    """`audio` (music) and `video_note` (round videos) follow the voice path."""

    def test_audio_message_is_transcribed_and_enqueued(self):
        update = {
            "update_id": 201,
            "message": {
                "message_id": 31,
                "chat": {"id": 28547271},
                "from": {"id": 28547271, "username": "luca"},
                "audio": {
                    "file_id": "AwACA-audio",
                    "mime_type": "audio/mpeg",
                    "duration": 12,
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            captured = _drive_telegram(instance, [update], transcript="great track")
            self.assertEqual(len(captured), 1)
            kwargs = captured[0]
            self.assertEqual(kwargs["content"], "great track")
            self.assertTrue(kwargs["meta"]["was_voice"])
            self.assertEqual(kwargs["meta"]["attachment_kind"], "audio")
            audio_path = Path(kwargs["meta"]["audio_path"])
            self.assertTrue(audio_path.exists())
            self.assertEqual(audio_path.suffix, ".mp3")

    def test_video_note_is_transcribed_and_enqueued(self):
        update = {
            "update_id": 202,
            "message": {
                "message_id": 32,
                "chat": {"id": 28547271},
                "from": {"id": 28547271, "username": "luca"},
                "video_note": {"file_id": "AwACA-vn", "duration": 4, "length": 240},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            captured = _drive_telegram(instance, [update], transcript="hi from the round")
            self.assertEqual(len(captured), 1)
            kwargs = captured[0]
            self.assertEqual(kwargs["content"], "hi from the round")
            self.assertEqual(kwargs["meta"]["attachment_kind"], "video_note")
            video_path = Path(kwargs["meta"]["audio_path"])
            self.assertTrue(video_path.exists())
            self.assertEqual(video_path.suffix, ".mp4")


class TelegramPhotoDocumentTests(unittest.TestCase):
    """Photos and documents enqueue with their local path threaded through meta."""

    def test_photo_largest_size_saved_with_caption(self):
        update = {
            "update_id": 301,
            "message": {
                "message_id": 41,
                "chat": {"id": 28547271},
                "from": {"id": 28547271, "username": "luca"},
                "caption": "look at this",
                "photo": [
                    {"file_id": "small", "width": 90, "height": 90},
                    {"file_id": "medium", "width": 320, "height": 320},
                    {"file_id": "large", "width": 1280, "height": 1280},
                ],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            captured = _drive_telegram(instance, [update])
            self.assertEqual(len(captured), 1)
            kwargs = captured[0]
            self.assertEqual(kwargs["content"], "look at this")
            self.assertNotIn("was_voice", kwargs["meta"])
            image_path = Path(kwargs["meta"]["image_path"])
            self.assertTrue(image_path.exists())
            self.assertTrue(
                image_path.is_relative_to(
                    instance / "state" / "voice" / "inbound" / "photos"
                )
            )
            self.assertEqual(image_path.suffix, ".jpg")

    def test_photo_without_caption_uses_placeholder(self):
        update = {
            "update_id": 302,
            "message": {
                "message_id": 42,
                "chat": {"id": 28547271},
                "from": {"id": 28547271, "username": "luca"},
                "photo": [{"file_id": "tiny", "width": 90, "height": 90}],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            captured = _drive_telegram(instance, [update])
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["content"], "[image]")
            self.assertIn("image_path", captured[0]["meta"])

    def test_document_saved_with_meta_file_path(self):
        update = {
            "update_id": 303,
            "message": {
                "message_id": 43,
                "chat": {"id": 28547271},
                "from": {"id": 28547271, "username": "luca"},
                "caption": "Q4 numbers",
                "document": {
                    "file_id": "DocAAAQ",
                    "file_name": "report.pdf",
                    "mime_type": "application/pdf",
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            captured = _drive_telegram(instance, [update])
            self.assertEqual(len(captured), 1)
            kwargs = captured[0]
            self.assertEqual(kwargs["content"], "Q4 numbers")
            file_path = Path(kwargs["meta"]["file_path"])
            self.assertTrue(file_path.exists())
            self.assertEqual(file_path.suffix, ".pdf")
            self.assertTrue(
                file_path.is_relative_to(
                    instance / "state" / "voice" / "inbound" / "docs"
                )
            )
            self.assertEqual(kwargs["meta"]["file_name"], "report.pdf")


class TelegramForwardDetectionTests(unittest.TestCase):
    def test_forward_logged_and_event_still_enqueued(self):
        update = {
            "update_id": 401,
            "message": {
                "message_id": 51,
                "chat": {"id": 28547271},
                "from": {"id": 28547271, "username": "luca"},
                "forward_from": {"id": 999, "username": "source_user"},
                "text": "look what they said",
            },
        }
        logs: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            captured = _drive_telegram(instance, [update], log=logs.append)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["content"], "look what they said")
            self.assertTrue(
                any("forward" in line and "source_user" in line for line in logs),
                f"forward log missing in {logs!r}",
            )


class TelegramGroupMentionTests(unittest.TestCase):
    """`_should_process_message` filters group/supergroup to @-mentions only."""

    def _channel(self, instance: Path, *, bot_username="rachelbot", bot_user_id=42):
        cfg = ChannelConfig(enabled=True, token_env="TELEGRAM_BOT_TOKEN")
        channel = TelegramChannel(instance, cfg, _silent_log)
        channel.token = "test-token"
        channel.bot_username = bot_username
        channel.bot_user_id = bot_user_id
        return channel

    def test_dm_replied_always(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel(Path(tmp))
            msg = {"chat": {"id": 28547271, "type": "private"}, "text": "hi"}
            self.assertTrue(channel._should_process_message(msg))

    def test_group_message_ignored_if_not_mentioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel(Path(tmp))
            msg = {"chat": {"id": -100, "type": "group"}, "text": "lunch later?"}
            self.assertFalse(channel._should_process_message(msg))

    def test_group_message_replied_if_mentioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel(Path(tmp))
            msg = {
                "chat": {"id": -100, "type": "group"},
                "text": "hey @rachelbot hi",
                "entities": [{"type": "mention", "offset": 4, "length": 10}],
            }
            self.assertTrue(channel._should_process_message(msg))

    def test_supergroup_mention_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel(Path(tmp), bot_username="rachelbot", bot_user_id=42)
            msg = {
                "chat": {"id": -1001, "type": "supergroup"},
                "text": "Rachel can you check this",
                "entities": [
                    {
                        "type": "text_mention",
                        "offset": 0,
                        "length": 6,
                        "user": {"id": 42, "first_name": "Rachel"},
                    }
                ],
            }
            self.assertTrue(channel._should_process_message(msg))

    def test_group_no_username_resolved_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel(Path(tmp), bot_username=None, bot_user_id=None)
            msg = {
                "chat": {"id": -100, "type": "group"},
                "text": "hey @rachelbot hi",
                "entities": [{"type": "mention", "offset": 4, "length": 10}],
            }
            self.assertFalse(channel._should_process_message(msg))

    def test_group_mention_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel(Path(tmp))
            # No entities supplied — exercises the substring fallback.
            msg = {
                "chat": {"id": -100, "type": "group"},
                "text": "hi @RachelBot",
            }
            self.assertTrue(channel._should_process_message(msg))

    def test_group_reply_to_bot_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel(Path(tmp), bot_user_id=42)
            msg = {
                "chat": {"id": -100, "type": "group"},
                "text": "thanks",
                "reply_to_message": {"from": {"id": 42, "username": "rachelbot"}},
            }
            self.assertTrue(channel._should_process_message(msg))

    def test_group_reply_to_human_not_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel(Path(tmp), bot_user_id=42)
            msg = {
                "chat": {"id": -100, "type": "group"},
                "text": "thanks",
                "reply_to_message": {"from": {"id": 999, "username": "alice"}},
            }
            # Need to also mock _get_chat_member_count to avoid HTTP.
            channel._get_chat_member_count = lambda _cid: None
            self.assertFalse(channel._should_process_message(msg))

    def test_group_with_two_members_replies_always(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel(Path(tmp))
            channel._get_chat_member_count = lambda _cid: 2
            msg = {
                "chat": {"id": -100, "type": "group"},
                "text": "lunch later?",
            }
            self.assertTrue(channel._should_process_message(msg))

    def test_group_with_three_members_requires_mention(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel(Path(tmp))
            channel._get_chat_member_count = lambda _cid: 3
            msg = {
                "chat": {"id": -100, "type": "group"},
                "text": "lunch later?",
            }
            self.assertFalse(channel._should_process_message(msg))

    def test_member_count_cache_short_circuits_second_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel(Path(tmp))
            calls: list[str] = []

            def fake_http_json(url, *, data=None, timeout=15, **_):
                calls.append(url)
                return {"ok": True, "result": 2}

            orig = telegram_module.http_json
            telegram_module.http_json = fake_http_json
            try:
                self.assertEqual(channel._get_chat_member_count("-100"), 2)
                self.assertEqual(channel._get_chat_member_count("-100"), 2)
            finally:
                telegram_module.http_json = orig
            self.assertEqual(len(calls), 1, "second call should hit cache")
            self.assertIn("getChatMemberCount", calls[0])

    def test_member_count_failure_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel(Path(tmp))

            def boom(*_a, **_k):
                raise RuntimeError("network down")

            orig = telegram_module.http_json
            telegram_module.http_json = boom
            try:
                self.assertIsNone(channel._get_chat_member_count("-100"))
            finally:
                telegram_module.http_json = orig


class TelegramGroupSessionReuseTests(unittest.TestCase):
    """Per-group `conversation_id` keys session reuse so each group keeps its own brain thread."""

    def _make_update(self, update_id: int, chat_id: int, text: str):
        return {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "chat": {"id": chat_id, "type": "supergroup"},
                "from": {"id": 11, "username": "luca"},
                "text": f"@rachelbot {text}",
                "entities": [{"type": "mention", "offset": 0, "length": 10}],
            },
        }

    def _drive(self, instance: Path, updates):
        served = {"done": False}

        def fake_http_json(url, *, data=None, timeout=15, **_):
            if "getUpdates" in url:
                if served["done"]:
                    return {"ok": True, "result": []}
                served["done"] = True
                return {"ok": True, "result": updates}
            if "getMe" in url:
                return {
                    "ok": True,
                    "result": {"id": 42, "username": "rachelbot"},
                }
            if "getChatMemberCount" in url:
                return {"ok": True, "result": 5}
            return {"ok": True, "result": {}}

        captured: list[dict] = []

        def enqueue(**kwargs):
            captured.append(kwargs)

        stop_after = {"done": False}

        cfg = ChannelConfig(enabled=True, token_env="TELEGRAM_BOT_TOKEN")
        channel = TelegramChannel(instance, cfg, _silent_log)
        channel.token = "test-token"

        orig = telegram_module.http_json
        telegram_module.http_json = fake_http_json
        try:
            thread = threading.Thread(
                target=channel.run,
                args=(enqueue, lambda: stop_after["done"]),
                daemon=True,
            )
            thread.start()
            for _ in range(40):
                if served["done"] and len(captured) >= len(updates):
                    break
                time.sleep(0.1)
            stop_after["done"] = True
            thread.join(timeout=3)
        finally:
            telegram_module.http_json = orig
        return captured

    def test_same_group_messages_share_conversation_id(self):
        updates = [
            self._make_update(1001, -777, "first"),
            self._make_update(1002, -777, "second"),
            self._make_update(1003, -888, "other group"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            captured = self._drive(Path(tmp), updates)
        self.assertEqual(len(captured), 3)
        conv_ids = [c["conversation_id"] for c in captured]
        self.assertEqual(conv_ids[0], "-777")
        self.assertEqual(conv_ids[1], "-777")
        self.assertEqual(conv_ids[2], "-888")
        self.assertNotEqual(conv_ids[0], conv_ids[2])


class TelegramSendTypingTests(unittest.TestCase):
    def test_send_typing_posts_chat_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            cfg = ChannelConfig(enabled=True, token_env="TELEGRAM_BOT_TOKEN")
            channel = TelegramChannel(instance, cfg, _silent_log)
            channel.token = "test-token"

            seen: list[dict[str, Any]] = []

            def fake_http_json(url, *, data=None, timeout=15, **_):
                seen.append({"url": url, "data": data, "timeout": timeout})
                return {"ok": True, "result": True}

            orig = telegram_module.http_json
            telegram_module.http_json = fake_http_json
            try:
                channel.send_typing("28547271", message_thread_id=7)
            finally:
                telegram_module.http_json = orig

            self.assertEqual(len(seen), 1)
            call = seen[0]
            self.assertIn("/sendChatAction", call["url"])
            self.assertEqual(call["data"]["chat_id"], "28547271")
            self.assertEqual(call["data"]["action"], "typing")
            self.assertEqual(call["data"]["message_thread_id"], 7)


class TypingLoopTests(unittest.TestCase):
    """`typing_loop` is the testable core of the runtime typing thread."""

    def test_calls_immediately_and_after_each_interval(self):
        stop = threading.Event()
        calls: list[float] = []
        clock = [0.0]

        def mono() -> float:
            return clock[0]

        wait_count = [0]

        def fake_wait(seconds: float) -> bool:
            clock[0] += seconds
            wait_count[0] += 1
            if wait_count[0] >= 2:
                stop.set()
                return True
            return False

        def send(chat_id, thread_id):
            calls.append(clock[0])

        runtime_module.typing_loop(
            send,
            stop,
            chat_id="123",
            message_thread_id=None,
            monotonic=mono,
            wait=fake_wait,
        )

        self.assertEqual(calls, [0.0, 4.0])

    def test_caps_at_max_seconds(self):
        stop = threading.Event()  # never set
        calls: list[float] = []
        clock = [0.0]

        def mono() -> float:
            return clock[0]

        def fake_wait(seconds: float) -> bool:
            clock[0] += seconds
            return False

        def send(chat_id, thread_id):
            calls.append(clock[0])

        runtime_module.typing_loop(
            send,
            stop,
            chat_id="123",
            message_thread_id=None,
            max_seconds=60.0,
            interval=4.0,
            monotonic=mono,
            wait=fake_wait,
        )

        # Immediate (t=0) + one call after each successful 4s wait, until the
        # 60s deadline is reached. That's 1 + (60/4) = 16 sends.
        self.assertEqual(len(calls), 16)
        self.assertLessEqual(clock[0], 60.0)

    def test_silent_when_send_raises(self):
        stop = threading.Event()
        clock = [0.0]
        wait_count = [0]

        def mono() -> float:
            return clock[0]

        def fake_wait(seconds: float) -> bool:
            clock[0] += seconds
            wait_count[0] += 1
            if wait_count[0] >= 2:
                stop.set()
                return True
            return False

        def boom(*_args, **_kwargs):
            raise RuntimeError("network down")

        # Must not raise.
        runtime_module.typing_loop(
            boom,
            stop,
            chat_id="123",
            message_thread_id=None,
            monotonic=mono,
            wait=fake_wait,
        )


class VoiceChannelSynthTests(unittest.TestCase):
    """Voice TTS adapter: load voice.json + call voice.synth.synthesize."""

    def test_voice_send_returns_path_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            ref_dir = instance / "voice" / "references"
            ref_dir.mkdir(parents=True)
            (ref_dir / "voice.json").write_text(
                json.dumps(
                    {
                        "voice": "qwen-tts-vc-rachel-test",
                        "target_model": "qwen3-tts-vc-realtime-test",
                    }
                ),
                encoding="utf-8",
            )

            captured: dict[str, Any] = {}

            def fake_synthesize(text, out_path, *, voice_id, target_model, **_):
                captured["text"] = text
                captured["voice_id"] = voice_id
                captured["target_model"] = target_model
                # Touch the file to mimic real synth writing OGG bytes.
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                Path(out_path).write_bytes(b"OggS\x00\x00")
                return Path(out_path)

            fake_module = type(sys)("voice.synth")
            fake_module.synthesize = fake_synthesize
            saved = sys.modules.get("voice.synth")
            sys.modules["voice.synth"] = fake_module
            try:
                channel = VoiceChannel(instance, ChannelConfig(enabled=True), _silent_log)
                result = channel.send("hello luca", {"chat_id": "123"})
            finally:
                if saved is not None:
                    sys.modules["voice.synth"] = saved
                else:
                    sys.modules.pop("voice.synth", None)

            self.assertIsNotNone(result)
            self.assertTrue(Path(result).exists())
            self.assertTrue(Path(result).is_relative_to(instance / "state" / "voice" / "outbound"))
            self.assertEqual(captured["text"], "hello luca")
            self.assertEqual(captured["voice_id"], "qwen-tts-vc-rachel-test")
            self.assertEqual(captured["target_model"], "qwen3-tts-vc-realtime-test")

    def test_voice_send_returns_none_on_missing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)  # no voice/references/voice.json
            channel = VoiceChannel(instance, ChannelConfig(enabled=True), _silent_log)
            self.assertIsNone(channel.send("hello", {"chat_id": "123"}))


class TelegramSendVoiceTests(unittest.TestCase):
    """TelegramChannel.send_voice: multipart upload + message_id parsing."""

    def test_telegram_send_voice_uploads_and_returns_message_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            ogg = instance / "out.ogg"
            ogg.write_bytes(b"OggS\x00\x00fakeopus")

            cfg = ChannelConfig(enabled=True, token_env="TELEGRAM_BOT_TOKEN")
            channel = TelegramChannel(instance, cfg, _silent_log)
            channel.token = "test-token"

            captured: dict[str, Any] = {}

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

            def fake_urlopen(req, timeout=30):
                captured["url"] = req.full_url
                captured["headers"] = dict(req.headers)
                captured["body"] = req.data
                payload = json.dumps(
                    {"ok": True, "result": {"message_id": 4906}}
                ).encode("utf-8")
                return FakeResp(payload)

            orig_urlopen = telegram_module.urllib.request.urlopen
            telegram_module.urllib.request.urlopen = fake_urlopen
            try:
                msg_id = channel.send_voice(
                    str(ogg),
                    {"chat_id": "28547271", "message_thread_id": 7},
                )
            finally:
                telegram_module.urllib.request.urlopen = orig_urlopen

            self.assertEqual(msg_id, "4906")
            self.assertIn("/sendVoice", captured["url"])
            content_type = captured["headers"].get("Content-type") or captured["headers"].get(
                "Content-Type"
            )
            self.assertTrue(content_type.startswith("multipart/form-data"))
            body = captured["body"]
            self.assertIn(b'name="chat_id"', body)
            self.assertIn(b"28547271", body)
            self.assertIn(b'name="message_thread_id"', body)
            self.assertIn(b'name="voice"', body)
            self.assertIn(b'filename="out.ogg"', body)
            self.assertIn(b"OggS\x00\x00fakeopus", body)


class TelegramSendParseModeTests(unittest.TestCase):
    """`TelegramChannel.send` posts MarkdownV2 with escaped text + 400 retry."""

    def _make_channel(self, instance: Path):
        cfg = ChannelConfig(enabled=True, token_env="TELEGRAM_BOT_TOKEN")
        channel = TelegramChannel(instance, cfg, _silent_log)
        channel.token = "test-token"
        return channel

    def _patch_http(self, captured: list[dict[str, Any]], result_id: int = 9999):
        def fake_http_json(url, *, data=None, timeout=15, **_):
            captured.append({"url": url, "data": data})
            return {"ok": True, "result": {"message_id": result_id}}

        return fake_http_json

    def test_send_sets_parse_mode_v2_and_escapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            channel = self._make_channel(instance)
            captured: list[dict[str, Any]] = []
            orig = telegram_module.http_json
            telegram_module.http_json = self._patch_http(captured)
            try:
                channel.send("Hello, world.", {"chat_id": "28547271"})
            finally:
                telegram_module.http_json = orig
        self.assertEqual(len(captured), 1)
        payload = captured[0]["data"]
        self.assertEqual(payload["parse_mode"], "MarkdownV2")
        # Period must arrive as `\.` so MarkdownV2 parses cleanly.
        self.assertEqual(payload["text"], "Hello, world\\.")
        self.assertTrue(payload["disable_web_page_preview"])

    def test_send_rewrites_bold_to_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            channel = self._make_channel(instance)
            captured: list[dict[str, Any]] = []
            orig = telegram_module.http_json
            telegram_module.http_json = self._patch_http(captured)
            try:
                channel.send("**bold** text", {"chat_id": "28547271"})
            finally:
                telegram_module.http_json = orig
        self.assertEqual(captured[0]["data"]["text"], "*bold* text")
        self.assertEqual(captured[0]["data"]["parse_mode"], "MarkdownV2")

    def test_parse_error_via_ok_false_retries_plain(self):
        original = "weird **stuff"  # malformed; pretend escaper bug
        calls: list[dict[str, Any]] = []

        def fake_http_json(url, *, data=None, timeout=15, **_):
            calls.append(data)
            if data.get("parse_mode") == "MarkdownV2":
                return {
                    "ok": False,
                    "description": "Bad Request: can't parse entities",
                }
            return {"ok": True, "result": {"message_id": 7777}}

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            channel = self._make_channel(instance)
            orig = telegram_module.http_json
            telegram_module.http_json = fake_http_json
            try:
                msg_id = channel.send(original, {"chat_id": "28547271"})
            finally:
                telegram_module.http_json = orig
        self.assertEqual(msg_id, "7777")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["parse_mode"], "MarkdownV2")
        self.assertNotIn("parse_mode", calls[1])
        self.assertEqual(calls[1]["text"], original)

    def test_parse_error_via_http_400_retries_plain(self):
        from urllib.error import HTTPError

        original = "Hello, world."
        calls: list[dict[str, Any]] = []

        def fake_http_json(url, *, data=None, timeout=15, **_):
            calls.append(data)
            if data.get("parse_mode") == "MarkdownV2":
                body = json.dumps(
                    {
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: can't parse entities: at byte 12",
                    }
                ).encode("utf-8")
                raise HTTPError(
                    url=url,
                    code=400,
                    msg="Bad Request",
                    hdrs=None,
                    fp=__import__("io").BytesIO(body),
                )
            return {"ok": True, "result": {"message_id": 8888}}

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            channel = self._make_channel(instance)
            orig = telegram_module.http_json
            telegram_module.http_json = fake_http_json
            try:
                msg_id = channel.send(original, {"chat_id": "28547271"})
            finally:
                telegram_module.http_json = orig
        self.assertEqual(msg_id, "8888")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("parse_mode", calls[1])
        self.assertEqual(calls[1]["text"], original)

    def test_non_parse_400_raises(self):
        from urllib.error import HTTPError

        def fake_http_json(url, *, data=None, timeout=15, **_):
            body = json.dumps(
                {"ok": False, "error_code": 400, "description": "Forbidden chat"}
            ).encode("utf-8")
            raise HTTPError(
                url=url,
                code=400,
                msg="Forbidden",
                hdrs=None,
                fp=__import__("io").BytesIO(body),
            )

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            channel = self._make_channel(instance)
            orig = telegram_module.http_json
            telegram_module.http_json = fake_http_json
            try:
                with self.assertRaises(RuntimeError):
                    channel.send("hi", {"chat_id": "28547271"})
            finally:
                telegram_module.http_json = orig


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
