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
from gateway import config as gateway_config  # noqa: E402
from gateway.channels import telegram as telegram_module  # noqa: E402
from gateway.channels.telegram import TelegramChannel  # noqa: E402
from gateway.config import ChannelConfig  # noqa: E402


def _write_minimal_yaml(instance: Path, chat_ids: list[str]) -> None:
    """Mirror static cfg.chat_ids into ops/gateway.yaml.

    Required because the config-only auth check reads from yaml/.env,
    not from the in-memory `ChannelConfig` passed to the constructor.
    """
    (instance / "ops").mkdir(exist_ok=True)
    flat = ", ".join(chat_ids) if chat_ids else ""
    (instance / "ops" / "gateway.yaml").write_text(
        "default_brain: claude\n"
        "channels:\n"
        "  telegram:\n"
        "    enabled: true\n"
        "    token_env: TELEGRAM_BOT_TOKEN\n"
        f"    chat_ids: [{flat}]\n"
    )
    gateway_config.clear_config_cache()


def _silent_log(_message: str) -> None:
    pass


def _drive(instance: Path, updates, *, exclude_from_allowlist=None, http_calls=None):
    served = {"done": False}

    def fake_http_json(url, *, data=None, timeout=15, **_):
        if http_calls is not None:
            http_calls.append({"url": url, "data": data})
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

    # Default-deny auth means every test update must come from a
    # pre-authorized chat. Lift each update's chat id into the env
    # allowlist so the channel routes them to the brain. (Unless
    # explicitly excluded to test auth denial.)
    exclude_from_allowlist = exclude_from_allowlist or set()
    chat_ids = sorted({
        str((u.get("message") or u.get("edited_message") or {}).get("chat", {}).get("id", ""))
        for u in updates
        if isinstance(u, dict)
    } - {""} - exclude_from_allowlist)
    yaml_chat_ids = sorted({"28547271", *chat_ids})
    cfg = ChannelConfig(
        enabled=True,
        token_env="TELEGRAM_BOT_TOKEN",
        chat_ids=chat_ids,
    )
    _write_minimal_yaml(instance, yaml_chat_ids)
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

    def test_group_recorded_even_when_not_mentioned(self):
        """Group messages without @-mention are not dispatched but should
        still appear in the chats directory (observability)."""
        update = {
            "update_id": 7,
            "message": {
                "message_id": 23,
                "chat": {"id": -200, "type": "group", "title": "Random chatter"},
                "from": {"id": 1, "username": "luca"},
                "text": "hello world",  # no mention, no reply-to-bot
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            captured = _drive(instance, [update])
            # Not enqueued — _should_process_message rejected it.
            self.assertEqual(captured, [])
            # But still recorded in the chats table.
            row = chats.get_chat(instance, channel="telegram", chat_id="-200")
            self.assertIsNotNone(row)
            self.assertEqual(row.chat_type, "group")
            self.assertEqual(row.title, "Random chatter")

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


class TelegramGroupAuthTests(unittest.TestCase):
    """Bot-added detection, auth prompt, callback handling, message gating."""

    def _channel(self, instance, *, main_chat="28547271"):
        cfg = ChannelConfig(enabled=True, token_env="TELEGRAM_BOT_TOKEN")
        channel = TelegramChannel(instance, cfg, _silent_log)
        channel.token = "test-token"
        channel.bot_username = "rachelbot"
        channel.bot_user_id = 42
        # Stub TELEGRAM_CHAT_ID resolution so _main_chat_id() returns our test value.
        channel._main_chat_id = lambda: main_chat
        return channel

    def _patch_http(self, channel, calls: list[dict]):
        def fake_http_json(url, *, data=None, timeout=15, **_):
            calls.append({"url": url, "data": data})
            if "getChatMemberCount" in url:
                return {"ok": True, "result": 5}
            if "getUpdates" in url:
                return {"ok": True, "result": []}
            return {"ok": True, "result": {"message_id": 9001}}

        orig = telegram_module.http_json
        telegram_module.http_json = fake_http_json
        return orig

    def test_my_chat_member_marks_pending_and_sends_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            channel = self._channel(instance)
            calls: list[dict] = []
            orig = self._patch_http(channel, calls)
            try:
                update = {
                    "update_id": 5000,
                    "my_chat_member": {
                        "chat": {"id": -1001, "type": "supergroup", "title": "BNESIM ops"},
                        "from": {"id": 999, "username": "alice", "first_name": "Alice"},
                        "new_chat_member": {
                            "user": {"id": 42, "username": "rachelbot"},
                            "status": "member",
                        },
                    },
                }
                channel._handle_my_chat_member(update)
            finally:
                telegram_module.http_json = orig
            row = chats.get_chat(instance, channel="telegram", chat_id="-1001")
            self.assertIsNotNone(row)
            self.assertEqual(row.auth_status, "pending")
            self.assertEqual(row.title, "BNESIM ops")
            send_calls = [c for c in calls if "sendMessage" in c["url"]]
            self.assertEqual(len(send_calls), 1)
            payload = send_calls[0]["data"]
            self.assertEqual(payload["chat_id"], "28547271")
            self.assertIn("inline_keyboard", payload["reply_markup"])
            self.assertIn("chat_auth:allow:-1001", payload["reply_markup"])
            self.assertIn("chat_auth:deny:-1001", payload["reply_markup"])

    def test_my_chat_member_left_blocks_in_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
                instance = Path(tmp)
                _write_minimal_yaml(instance, ["-1001"])
                channel = self._channel(instance)
                chats.upsert_chat(
                    instance, channel="telegram", chat_id="-1001",
                    title="x",
                )
                calls: list[dict] = []
                orig = self._patch_http(channel, calls)
                try:
                    update = {
                        "update_id": 5001,
                        "my_chat_member": {
                            "chat": {"id": -1001, "type": "supergroup"},
                            "from": {"id": 999},
                            "new_chat_member": {
                                "user": {"id": 42, "username": "rachelbot"},
                                "status": "kicked",
                            },
                        },
                    }
                    channel._handle_my_chat_member(update)
                finally:
                    telegram_module.http_json = orig
                from gateway.config import load_config
                cfg = load_config(instance).channel("telegram")
                self.assertIn("-1001", cfg.blocked_chat_ids)
                self.assertNotIn("-1001", cfg.chat_ids)

    def test_my_chat_member_for_other_user_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            channel = self._channel(instance)
            calls: list[dict] = []
            orig = self._patch_http(channel, calls)
            try:
                update = {
                    "my_chat_member": {
                        "chat": {"id": -1001, "type": "supergroup"},
                        "new_chat_member": {
                            "user": {"id": 7777, "username": "alice"},
                            "status": "member",
                        },
                    },
                }
                channel._handle_my_chat_member(update)
            finally:
                telegram_module.http_json = orig
            row = chats.get_chat(instance, channel="telegram", chat_id="-1001")
            self.assertIsNone(row)

    def test_new_chat_members_backstop_path(self):
        update = {
            "update_id": 5002,
            "message": {
                "message_id": 17,
                "chat": {"id": -1002, "type": "group", "title": "Cardcentric"},
                "from": {"id": 999, "username": "alice"},
                "new_chat_members": [{"id": 42, "username": "rachelbot"}],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _drive(instance, [update])
            row = chats.get_chat(instance, channel="telegram", chat_id="-1002")
            self.assertIsNotNone(row)
            self.assertEqual(row.auth_status, "pending")

    def test_callback_allow_writes_config_and_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_minimal_yaml(instance, [])
            channel = self._channel(instance)
            chats.upsert_chat(
                instance, channel="telegram", chat_id="-1001",
                title="BNESIM ops",
            )
            calls: list[dict] = []
            orig = self._patch_http(channel, calls)
            try:
                update = {
                    "update_id": 5100,
                    "callback_query": {
                        "id": "cb1",
                        "from": {"id": 28547271, "username": "luca"},
                        "data": "chat_auth:allow:-1001",
                        "message": {
                            "message_id": 200,
                            "chat": {"id": 28547271, "type": "private"},
                        },
                    },
                }
                channel._handle_callback_query(update)
            finally:
                telegram_module.http_json = orig
            from gateway.config import load_config
            from gateway.config_writer import env_chat_ids as _env
            cfg = load_config(instance).channel("telegram")
            self.assertIn("-1001", cfg.chat_ids)
            self.assertIn("-1001", _env(instance))
            urls = [c["url"] for c in calls]
            self.assertTrue(any("editMessageText" in u for u in urls))
            self.assertTrue(any("answerCallbackQuery" in u for u in urls))
            self.assertFalse(any("leaveChat" in u for u in urls))

    def test_callback_deny_writes_blocklist_and_leaves(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_minimal_yaml(instance, [])
            channel = self._channel(instance)
            chats.upsert_chat(
                instance, channel="telegram", chat_id="-1001",
                title="BNESIM ops",
            )
            calls: list[dict] = []
            orig = self._patch_http(channel, calls)
            try:
                update = {
                    "callback_query": {
                        "id": "cb2",
                        "from": {"id": 28547271},
                        "data": "chat_auth:deny:-1001",
                        "message": {
                            "message_id": 201,
                            "chat": {"id": 28547271},
                        },
                    },
                }
                channel._handle_callback_query(update)
            finally:
                telegram_module.http_json = orig
            from gateway.config import load_config
            cfg = load_config(instance).channel("telegram")
            self.assertIn("-1001", cfg.blocked_chat_ids)
            self.assertNotIn("-1001", cfg.chat_ids)
            urls = [c["url"] for c in calls]
            self.assertTrue(any("leaveChat" in u for u in urls))

    def test_callback_from_unauthorized_user_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_minimal_yaml(instance, [])
            channel = self._channel(instance)
            chats.upsert_chat(
                instance, channel="telegram", chat_id="-1001",
            )
            calls: list[dict] = []
            orig = self._patch_http(channel, calls)
            try:
                update = {
                    "callback_query": {
                        "id": "cb-bad",
                        "from": {"id": 99999},  # not the operator
                        "data": "chat_auth:allow:-1001",
                        "message": {"message_id": 1, "chat": {"id": 99999}},
                    },
                }
                channel._handle_callback_query(update)
            finally:
                telegram_module.http_json = orig
            # No auth-state mutation should have occurred.
            from gateway.config import load_config
            cfg = load_config(instance).channel("telegram")
            self.assertNotIn("-1001", cfg.chat_ids)
            self.assertNotIn("-1001", cfg.blocked_chat_ids)
            urls = [c["url"] for c in calls]
            self.assertTrue(any("answerCallbackQuery" in u for u in urls))
            self.assertFalse(any("editMessageText" in u for u in urls))

    def test_pending_chat_message_dropped(self):
        update = {
            "update_id": 6000,
            "message": {
                "message_id": 50,
                "chat": {"id": -2001, "type": "supergroup", "title": "X"},
                "from": {"id": 1, "username": "luca"},
                "text": "@rachelbot hello",
                "entities": [{"type": "mention", "offset": 0, "length": 10}],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            calls: list[dict] = []
            captured = _drive(
                instance,
                [update],
                exclude_from_allowlist={"-2001"},
                http_calls=calls,
            )
            self.assertEqual(len(captured), 0)
            send_calls = [c for c in calls if "sendMessage" in c["url"]]
            self.assertEqual(len(send_calls), 1)
            self.assertEqual(send_calls[0]["data"]["chat_id"], "28547271")
            self.assertIn("chat_auth:allow:-2001", send_calls[0]["data"]["reply_markup"])

    def test_allowed_chat_message_processed(self):
        update = {
            "update_id": 6001,
            "message": {
                "message_id": 51,
                "chat": {"id": -2002, "type": "supergroup", "title": "Y"},
                "from": {"id": 1, "username": "luca"},
                "text": "@rachelbot ping",
                "entities": [{"type": "mention", "offset": 0, "length": 10}],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            captured = _drive(instance, [update])
            self.assertEqual(len(captured), 1)

    def test_yaml_allowlist_authorizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_minimal_yaml(instance, ["-3000"])
            cfg = ChannelConfig(
                enabled=True,
                token_env="TELEGRAM_BOT_TOKEN",
                chat_ids=["-3000"],
            )
            channel = TelegramChannel(instance, cfg, _silent_log)
            channel.token = "test-token"
            channel.bot_username = "rachelbot"
            channel.bot_user_id = 42
            self.assertTrue(channel._is_authorized("-3000"))


if __name__ == "__main__":
    unittest.main()
