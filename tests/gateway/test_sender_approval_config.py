"""Tests for the config-only Telegram sender-approval flow.

Covers the auth check (yaml `chat_ids` + .env `TELEGRAM_CHAT_IDS` +
yaml `blocked_chat_ids`) and the approve/reject callback writers.

Spec: docs/specs/sender-approval-config-only.md
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from gateway import chats as chats_module  # noqa: E402
from gateway import config as gateway_config  # noqa: E402
from gateway import config_writer  # noqa: E402
from gateway import queue  # noqa: E402
from gateway.channels.telegram import TelegramChannel  # noqa: E402
from gateway.config import ChannelConfig  # noqa: E402


def _make_instance(yaml_chat_ids: list[str] | None = None,
                   yaml_blocked: list[str] | None = None,
                   env_chat_ids: list[str] | None = None,
                   main_chat_id: str = "28547271") -> Path:
    """Spin up a minimal gateway instance with the requested config."""
    root = Path(tempfile.mkdtemp(prefix="approval-config-"))
    (root / "ops").mkdir()
    chat_ids_yaml = yaml_chat_ids if yaml_chat_ids is not None else [main_chat_id]
    blocked_yaml = yaml_blocked if yaml_blocked is not None else []
    env_chats = env_chat_ids if env_chat_ids is not None else []
    yaml_path = root / "ops" / "gateway.yaml"
    yaml_path.write_text(
        "default_brain: claude\n"
        "channels:\n"
        "  telegram:\n"
        "    enabled: true\n"
        "    token_env: TELEGRAM_BOT_TOKEN\n"
        f"    chat_ids: [{', '.join(chat_ids_yaml)}]\n"
        + (f"    blocked_chat_ids: [{', '.join(blocked_yaml)}]\n"
           if blocked_yaml else "")
    )
    env_lines = [
        "TELEGRAM_BOT_TOKEN=test-token-123",
        f"TELEGRAM_CHAT_ID='{main_chat_id}'",
    ]
    if env_chats:
        env_lines.append(f"TELEGRAM_CHAT_IDS='{','.join(env_chats)}'")
    (root / ".env").write_text("\n".join(env_lines) + "\n")
    queue.connect(root)
    # Caches are global module-state; clear so prior tests don't leak.
    gateway_config.clear_config_cache()
    gateway_config.clear_env_cache()
    return root


def _make_channel(instance: Path, cfg_chat_ids: list[str] | None = None,
                  main_chat_id: str = "28547271") -> TelegramChannel:
    cfg = ChannelConfig(
        enabled=True,
        chat_ids=tuple(cfg_chat_ids or [main_chat_id]),
        token_env="TELEGRAM_BOT_TOKEN",
    )
    channel = TelegramChannel(instance_dir=instance, cfg=cfg, log=MagicMock())
    return channel


class AuthCheckTest(unittest.TestCase):
    def test_yaml_chat_ids_authorize(self):
        instance = _make_instance(yaml_chat_ids=["12345", "28547271"])
        channel = _make_channel(instance, cfg_chat_ids=["12345", "28547271"])
        try:
            self.assertTrue(channel._is_authorized("12345"))
            self.assertTrue(channel._is_authorized("28547271"))
        finally:
            channel.close()

    def test_env_chat_ids_authorize(self):
        instance = _make_instance(
            yaml_chat_ids=["28547271"],
            env_chat_ids=["77777", "88888"],
        )
        channel = _make_channel(instance)
        try:
            self.assertTrue(channel._is_authorized("77777"))
            self.assertTrue(channel._is_authorized("88888"))
        finally:
            channel.close()

    def test_blocklist_short_circuits_allowlist(self):
        instance = _make_instance(
            yaml_chat_ids=["12345"],
            yaml_blocked=["12345"],
        )
        channel = _make_channel(instance, cfg_chat_ids=["12345"])
        try:
            self.assertFalse(channel._is_authorized("12345"))
        finally:
            channel.close()

    def test_unknown_chat_default_deny(self):
        instance = _make_instance()
        channel = _make_channel(instance)
        try:
            self.assertFalse(channel._is_authorized("99999"))
        finally:
            channel.close()

    def test_no_sqlite_reads_on_auth_path(self):
        """`_is_authorized` must not call `chats_module.get_chat`."""
        instance = _make_instance(yaml_chat_ids=["12345"])
        channel = _make_channel(instance, cfg_chat_ids=["12345"])
        try:
            with patch.object(
                chats_module, "get_chat", side_effect=AssertionError(
                    "auth path must not query the chats DB"
                )
            ):
                self.assertTrue(channel._is_authorized("12345"))
                self.assertFalse(channel._is_authorized("99999"))
        finally:
            channel.close()


class ApproveRejectTest(unittest.TestCase):
    def _send_callback(self, channel: TelegramChannel, action: str,
                       target: str, from_id: str = "28547271") -> None:
        update = {
            "callback_query": {
                "id": "cq-1",
                "data": f"chat_auth:{action}:{target}",
                "from": {"id": int(from_id)},
                "message": {"chat": {"id": 28547271}, "message_id": 42},
            }
        }
        with patch("gateway.channels.telegram.http_json") as mock_http:
            mock_http.return_value = {"ok": True, "result": {}}
            channel._handle_callback_query(update)

    def test_approve_writes_yaml_and_env(self):
        instance = _make_instance()
        channel = _make_channel(instance)
        try:
            self._send_callback(channel, "allow", "99999")
            cfg = gateway_config.load_config(instance).channel("telegram")
            self.assertIn("99999", cfg.chat_ids)
            self.assertIn("99999", config_writer.env_chat_ids(instance))
        finally:
            channel.close()

    def test_approve_atomic_via_tmp_replace(self):
        """Mid-write crash must not leave gateway.yaml partial.

        We patch `os.replace` to raise. The callback handler swallows
        the error (logs + answer "write failed"); the original yaml
        must remain untouched even though the temp file got written.
        """
        instance = _make_instance()
        channel = _make_channel(instance)
        original_yaml = (instance / "ops" / "gateway.yaml").read_text()
        try:
            with patch("gateway.config_writer.os.replace",
                       side_effect=RuntimeError("simulated crash")):
                self._send_callback(channel, "allow", "99999")
            self.assertEqual(
                (instance / "ops" / "gateway.yaml").read_text(),
                original_yaml,
            )
        finally:
            channel.close()

    def test_reject_writes_blocklist_only(self):
        instance = _make_instance()
        channel = _make_channel(instance)
        env_before = (instance / ".env").read_text()
        try:
            self._send_callback(channel, "deny", "88888")
            cfg = gateway_config.load_config(instance).channel("telegram")
            self.assertIn("88888", cfg.blocked_chat_ids)
            self.assertNotIn("88888", cfg.chat_ids)
            self.assertEqual((instance / ".env").read_text(), env_before)
        finally:
            channel.close()

    def test_approve_idempotent(self):
        instance = _make_instance(yaml_chat_ids=["28547271", "12345"],
                                  env_chat_ids=["12345"])
        channel = _make_channel(instance, cfg_chat_ids=["28547271", "12345"])
        yaml_path = instance / "ops" / "gateway.yaml"
        env_path = instance / ".env"
        m_yaml = yaml_path.stat().st_mtime
        m_env = env_path.stat().st_mtime
        try:
            self._send_callback(channel, "allow", "12345")
            self.assertEqual(yaml_path.stat().st_mtime, m_yaml)
            self.assertEqual(env_path.stat().st_mtime, m_env)
        finally:
            channel.close()

    def test_blocked_then_approved_promotion(self):
        instance = _make_instance(yaml_blocked=["55555"])
        channel = _make_channel(instance)
        try:
            self.assertFalse(channel._is_authorized("55555"))
            self._send_callback(channel, "allow", "55555")
            cfg = gateway_config.load_config(instance).channel("telegram")
            self.assertIn("55555", cfg.chat_ids)
            self.assertNotIn("55555", cfg.blocked_chat_ids)
            self.assertTrue(channel._is_authorized("55555"))
        finally:
            channel.close()

    def test_approve_rejected_for_non_operator(self):
        """Callback from a non-operator user must not write config."""
        instance = _make_instance()
        channel = _make_channel(instance)
        yaml_path = instance / "ops" / "gateway.yaml"
        m_yaml = yaml_path.stat().st_mtime
        try:
            self._send_callback(
                channel, "allow", "99999", from_id="11111"  # not the operator
            )
            self.assertEqual(yaml_path.stat().st_mtime, m_yaml)
            cfg = gateway_config.load_config(instance).channel("telegram")
            self.assertNotIn("99999", cfg.chat_ids)
        finally:
            channel.close()


class HotReloadTest(unittest.TestCase):
    def test_external_yaml_edit_picked_up(self):
        instance = _make_instance(yaml_chat_ids=["28547271"])
        channel = _make_channel(instance)
        try:
            self.assertFalse(channel._is_authorized("44444"))
            yaml_path = instance / "ops" / "gateway.yaml"
            yaml_path.write_text(
                "default_brain: claude\n"
                "channels:\n"
                "  telegram:\n"
                "    enabled: true\n"
                "    token_env: TELEGRAM_BOT_TOKEN\n"
                "    chat_ids: [28547271, 44444]\n"
            )
            # Bump mtime by 2s so the cache invalidates even on
            # filesystems with 1s resolution.
            future = yaml_path.stat().st_mtime + 2
            os.utime(yaml_path, (future, future))
            self.assertTrue(channel._is_authorized("44444"))
        finally:
            channel.close()


class ConfigWriterUnitTest(unittest.TestCase):
    """Direct tests for config_writer that don't need the channel."""

    def test_env_writer_idempotent(self):
        instance = _make_instance(env_chat_ids=["111", "222"])
        self.assertFalse(
            config_writer.update_env_chat_ids(instance, add=["111"])
        )

    def test_env_writer_appends(self):
        instance = _make_instance(env_chat_ids=["111"])
        self.assertTrue(
            config_writer.update_env_chat_ids(instance, add=["222"])
        )
        self.assertEqual(
            config_writer.env_chat_ids(instance),
            ("111", "222"),
        )

    def test_env_writer_creates_file_on_add(self):
        root = Path(tempfile.mkdtemp(prefix="env-writer-"))
        self.assertTrue(
            config_writer.update_env_chat_ids(root, add=["123"])
        )
        self.assertEqual(config_writer.env_chat_ids(root), ("123",))

    def test_env_writer_remove(self):
        instance = _make_instance(env_chat_ids=["111", "222", "333"])
        self.assertTrue(
            config_writer.update_env_chat_ids(instance, remove=["222"])
        )
        self.assertEqual(
            config_writer.env_chat_ids(instance),
            ("111", "333"),
        )

    def test_yaml_writer_idempotent(self):
        instance = _make_instance(yaml_chat_ids=["111"])
        self.assertFalse(
            config_writer.update_gateway_yaml_chat_lists(
                instance, channel="telegram", allow_add=["111"],
            )
        )

    def test_yaml_writer_drops_empty_blocked(self):
        instance = _make_instance(
            yaml_chat_ids=["111"], yaml_blocked=["222"]
        )
        config_writer.update_gateway_yaml_chat_lists(
            instance, channel="telegram", block_remove=["222"],
        )
        text = (instance / "ops" / "gateway.yaml").read_text()
        self.assertNotIn("blocked_chat_ids", text)


if __name__ == "__main__":
    unittest.main()
