"""Telegram channel writes a chat row for every inbound message."""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import chats  # noqa: E402
from gateway.channels import telegram as telegram_module  # noqa: E402
from gateway.channels.telegram import TelegramChannel  # noqa: E402
from gateway.config import ChannelConfig  # noqa: E402


def _silent_log(_message: str) -> None:
    pass


def _drive(instance: Path, updates):
    served = {"done": False}

    def fake_http_json(url, *, data=None, timeout=15, **_):
        if "getUpdates" in url:
            if served["done"]:
                return {"ok": True, "result": []}
            served["done"] = True
            return {"ok": True, "result": updates}
        if "getMe" in url:
            return {"ok": True, "result": {"id": 42, "username": "rachelbot"}}
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


class TelegramChatRecordingTests(unittest.TestCase):
    def test_dm_recorded_with_first_last_name(self):
        update = {
            "update_id": 1,
            "message": {
                "message_id": 17,
                "chat": {
                    "id": 28547271,
                    "type": "private",
                    "first_name": "Luca",
                    "last_name": "Mattei",
                    "username": "luca",
                },
                "from": {"id": 28547271, "username": "luca"},
                "text": "hi",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _drive(instance, [update])
            row = chats.get_chat(instance, channel="telegram", chat_id="28547271")
            self.assertIsNotNone(row)
            self.assertEqual(row.chat_type, "private")
            self.assertEqual(row.title, "Luca Mattei")
            self.assertEqual(row.username, "luca")
            self.assertEqual(row.last_message_id, "17")

    def test_dm_falls_back_to_username_when_names_missing(self):
        update = {
            "update_id": 2,
            "message": {
                "message_id": 18,
                "chat": {"id": 99, "type": "private", "username": "ghost"},
                "from": {"id": 99, "username": "ghost"},
                "text": "hi",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _drive(instance, [update])
            row = chats.get_chat(instance, channel="telegram", chat_id="99")
            self.assertEqual(row.title, "@ghost")

    def test_group_recorded_with_title(self):
        update = {
            "update_id": 3,
            "message": {
                "message_id": 19,
                "chat": {"id": -100, "type": "group", "title": "BNESIM ops"},
                "from": {"id": 1, "username": "luca"},
                "text": "@rachelbot status",
                "entities": [{"type": "mention", "offset": 0, "length": 10}],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _drive(instance, [update])
            row = chats.get_chat(instance, channel="telegram", chat_id="-100")
            self.assertIsNotNone(row)
            self.assertEqual(row.chat_type, "group")
            self.assertEqual(row.title, "BNESIM ops")
            # Member count should be cached from _should_process_message's
            # 1:1 detection → recorded into the row.
            self.assertEqual(row.member_count, 5)

    def test_supergroup_recorded(self):
        update = {
            "update_id": 4,
            "message": {
                "message_id": 20,
                "chat": {
                    "id": -1001,
                    "type": "supergroup",
                    "title": "Cardcentric",
                    "username": "cardcentric_chat",
                },
                "from": {"id": 1, "username": "luca"},
                "text": "@rachelbot ping",
                "entities": [{"type": "mention", "offset": 0, "length": 10}],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _drive(instance, [update])
            row = chats.get_chat(instance, channel="telegram", chat_id="-1001")
            self.assertIsNotNone(row)
            self.assertEqual(row.chat_type, "supergroup")
            self.assertEqual(row.title, "Cardcentric")
            self.assertEqual(row.username, "cardcentric_chat")

    def test_channel_recorded(self):
        update = {
            "update_id": 5,
            "message": {
                "message_id": 21,
                "chat": {
                    "id": -1002,
                    "type": "channel",
                    "title": "BNESIM Announcements",
                },
                "from": {"id": 1, "username": "luca"},
                "text": "post",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _drive(instance, [update])
            row = chats.get_chat(instance, channel="telegram", chat_id="-1002")
            self.assertIsNotNone(row)
            self.assertEqual(row.chat_type, "channel")
            self.assertEqual(row.title, "BNESIM Announcements")

    def test_recording_failure_does_not_block_enqueue(self):
        update = {
            "update_id": 6,
            "message": {
                "message_id": 22,
                "chat": {"id": 7, "type": "private", "first_name": "X"},
                "from": {"id": 7},
                "text": "hi",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)

            calls: list[str] = []

            def boom(*_a, **_k):
                calls.append("called")
                raise RuntimeError("disk full")

            orig = telegram_module.chats_module.upsert_chat
            telegram_module.chats_module.upsert_chat = boom
            try:
                captured = _drive(instance, [update])
            finally:
                telegram_module.chats_module.upsert_chat = orig
            self.assertEqual(len(captured), 1)
            self.assertEqual(calls, ["called"])


if __name__ == "__main__":
    unittest.main()
