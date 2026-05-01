"""Tests for Telegram sender approval prompt rendering + suppression.

The auth-decision logic itself lives in
`test_sender_approval_config.py` (config-only flow). This module
covers the inline-keyboard prompt: when it fires, what it contains,
and the duplicate-suppression cache.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from gateway import config as gateway_config  # noqa: E402
from gateway import queue  # noqa: E402
from gateway.channels.telegram import TelegramChannel  # noqa: E402
from gateway.config import ChannelConfig  # noqa: E402


def _make_instance(yaml_chat_ids: list[str] | None = None,
                   yaml_blocked: list[str] | None = None,
                   main_chat_id: str = "28547271") -> Path:
    root = Path(tempfile.mkdtemp(prefix="approval-prompt-"))
    (root / "ops").mkdir()
    chat_ids_yaml = yaml_chat_ids if yaml_chat_ids is not None else [main_chat_id]
    blocked_yaml = yaml_blocked if yaml_blocked is not None else []
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
    (root / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=test-token-123\n"
        f"TELEGRAM_CHAT_ID='{main_chat_id}'\n"
    )
    queue.connect(root)
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
    return TelegramChannel(instance_dir=instance, cfg=cfg, log=MagicMock())


class TelegramSenderApprovalPromptTest(unittest.TestCase):
    def test_new_pending_sender_triggers_prompt(self) -> None:
        instance = _make_instance()
        channel = _make_channel(instance)
        try:
            channel._record_chat(
                chat={"id": 1234, "type": "private", "username": "alice"},
                message={"message_id": 100, "text": "hello"},
            )
            with patch("gateway.channels.telegram.http_json") as mock_http:
                mock_http.return_value = {"ok": True, "result": {"message_id": 999}}
                channel._maybe_send_sender_approval_prompt(
                    chat_id="1234",
                    chat={"id": 1234, "type": "private", "username": "alice"},
                    message={"text": "hello", "message_id": 100},
                )
                assert mock_http.called
                payload = mock_http.call_args.kwargs["data"]
                self.assertIn("New contact", payload["text"])
                self.assertIn("alice", payload["text"])
                self.assertIn("1234", payload["text"])
        finally:
            channel.close()

    def test_already_allowed_sender_no_prompt(self) -> None:
        instance = _make_instance(yaml_chat_ids=["28547271", "1234"])
        channel = _make_channel(instance, cfg_chat_ids=["28547271", "1234"])
        try:
            with patch("gateway.channels.telegram.http_json") as mock_http:
                channel._maybe_send_sender_approval_prompt(
                    chat_id="1234",
                    chat={"id": 1234, "type": "private", "username": "alice"},
                    message={"text": "hello"},
                )
                self.assertFalse(mock_http.called)
        finally:
            channel.close()

    def test_blocked_sender_no_prompt(self) -> None:
        instance = _make_instance(
            yaml_chat_ids=["28547271"], yaml_blocked=["1234"],
        )
        channel = _make_channel(instance)
        try:
            with patch("gateway.channels.telegram.http_json") as mock_http:
                channel._maybe_send_sender_approval_prompt(
                    chat_id="1234",
                    chat={"id": 1234, "type": "private", "username": "eve"},
                    message={"text": "hello"},
                )
                self.assertFalse(mock_http.called)
        finally:
            channel.close()

    def test_duplicate_prompt_suppressed(self) -> None:
        instance = _make_instance()
        channel = _make_channel(instance)
        try:
            with patch("gateway.channels.telegram.http_json") as mock_http:
                mock_http.return_value = {"ok": True, "result": {"message_id": 999}}
                channel._record_chat(
                    chat={"id": 5555, "type": "private", "username": "bob"},
                    message={"message_id": 100, "text": "first"},
                )
                channel._maybe_send_sender_approval_prompt(
                    chat_id="5555",
                    chat={"id": 5555, "type": "private", "username": "bob"},
                    message={"text": "first"},
                )
                self.assertEqual(mock_http.call_count, 1)
                channel._maybe_send_sender_approval_prompt(
                    chat_id="5555",
                    chat={"id": 5555, "type": "private", "username": "bob"},
                    message={"text": "second"},
                )
                self.assertEqual(mock_http.call_count, 1)
        finally:
            channel.close()

    def test_message_preview_in_prompt(self) -> None:
        instance = _make_instance()
        channel = _make_channel(instance)
        try:
            with patch("gateway.channels.telegram.http_json") as mock_http:
                mock_http.return_value = {"ok": True, "result": {"message_id": 999}}
                channel._record_chat(
                    chat={"id": 6666, "type": "private"},
                    message={"message_id": 100, "text": "important business idea"},
                )
                channel._maybe_send_sender_approval_prompt(
                    chat_id="6666",
                    chat={"id": 6666, "type": "private"},
                    message={"text": "important business idea"},
                )
                payload = mock_http.call_args.kwargs["data"]
                self.assertIn("important", payload["text"])
        finally:
            channel.close()

    def test_main_chat_falls_back_to_yaml_chat_ids(self) -> None:
        instance = _make_instance(yaml_chat_ids=["28547271"])
        (instance / ".env").write_text("TELEGRAM_BOT_TOKEN=test-token-123\n")
        gateway_config.clear_env_cache()
        channel = _make_channel(instance)
        try:
            with patch("gateway.channels.telegram.http_json") as mock_http:
                mock_http.return_value = {"ok": True, "result": {"message_id": 999}}
                channel._maybe_send_sender_approval_prompt(
                    chat_id="1234",
                    chat={"id": 1234, "type": "private", "username": "alice"},
                    message={"text": "hello"},
                )
                payload = mock_http.call_args.kwargs["data"]
                self.assertEqual(payload["chat_id"], "28547271")
        finally:
            channel.close()

    def test_rejected_prompt_is_retryable(self) -> None:
        instance = _make_instance()
        channel = _make_channel(instance)
        try:
            with patch("gateway.channels.telegram.http_json") as mock_http:
                mock_http.side_effect = [
                    {"ok": False, "description": "bad request"},
                    {"ok": True, "result": {"message_id": 999}},
                ]
                for text in ("first", "second"):
                    channel._maybe_send_sender_approval_prompt(
                        chat_id="1234",
                        chat={"id": 1234, "type": "private", "username": "alice"},
                        message={"text": text},
                    )
                self.assertEqual(mock_http.call_count, 2)
        finally:
            channel.close()

    def test_auth_prompt_uses_plain_text_payload(self) -> None:
        instance = _make_instance()
        channel = _make_channel(instance)
        try:
            with patch("gateway.channels.telegram.http_json") as mock_http:
                mock_http.return_value = {"ok": True, "result": {"message_id": 999}}
                channel._maybe_send_sender_approval_prompt(
                    chat_id="1234",
                    chat={"id": 1234, "type": "private", "username": "alice"},
                    message={"text": "hello_world"},
                )
                payload = mock_http.call_args.kwargs["data"]
                self.assertNotIn("parse_mode", payload)
                self.assertIn('Preview: "hello_world"', payload["text"])
        finally:
            channel.close()


if __name__ == "__main__":
    unittest.main()
