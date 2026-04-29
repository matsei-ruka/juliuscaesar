"""Tests for Telegram sender approval prompt feature."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from gateway import chats as chats_module
from gateway import queue


def _make_instance() -> Path:
    """Create a minimal test instance with queues DB."""
    root = Path(tempfile.mkdtemp(prefix="telegram-auth-test-"))
    (root / "ops").mkdir()

    # Create .env with required tokens
    (root / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=test-token-123\n"
        "TELEGRAM_CHAT_ID=28547271\n"
    )

    # Initialize chats DB
    queue.connect(root)
    return root


class TelegramSenderApprovalTest(unittest.TestCase):
    """Test sender approval prompt feature."""

    def test_new_pending_sender_triggers_prompt(self) -> None:
        """Unauthorized sender marked 'pending' → approval prompt sent."""
        instance = _make_instance()

        # Create a mock TelegramChannel with just the methods we need
        from gateway.channels.telegram import TelegramChannel
        from gateway.config import ChannelConfig

        cfg = ChannelConfig(
            enabled=True,
            chat_ids=["9999"],  # Different, not authorized
            token_env="TELEGRAM_BOT_TOKEN",
        )
        log_func = MagicMock()

        channel = TelegramChannel(
            instance_dir=instance,
            cfg=cfg,
            log=log_func,
        )

        # Record the chat (defaults to 'pending')
        channel._record_chat(
            chat={"id": 1234, "type": "private", "username": "alice"},
            message={"message_id": 100, "text": "hello"},
        )

        # Mock HTTP to avoid actual network calls
        with patch("gateway.channels.telegram.http_json") as mock_http:
            mock_http.return_value = {"ok": True, "result": {"message_id": 999}}

            # Call the approval prompt logic
            channel._maybe_send_sender_approval_prompt(
                chat_id="1234",
                chat={"id": 1234, "type": "private", "username": "alice"},
                message={"text": "hello", "message_id": 100},
            )

            # Verify HTTP was called (prompt sent)
            assert mock_http.called
            call_args = mock_http.call_args
            payload = call_args.kwargs["data"]
            assert "🔐 New contact" in payload["text"]
            assert "@alice" in payload["text"]
            assert "1234" in payload["text"]

        channel.close()

    def test_already_approved_sender_no_prompt(self) -> None:
        """Already-approved sender → no approval prompt."""
        instance = _make_instance()
        conn = queue.connect(instance)  # Uses same DB initialization

        # Insert approved chat
        chats_module.upsert_chat(
            conn=conn,
            channel="telegram",
            chat_id="1234",
            chat_type="private",
            title="alice",
            auth_status="allowed",
        )
        conn.commit()

        from gateway.channels.telegram import TelegramChannel
        from gateway.config import ChannelConfig

        cfg = ChannelConfig(
            enabled=True,
            chat_ids=["9999"],
            token_env="TELEGRAM_BOT_TOKEN",
        )

        with patch("gateway.channels.telegram.http_json") as mock_http:
            channel = TelegramChannel(
                instance_dir=instance,
                cfg=cfg,
                log=MagicMock(),
            )

            # Should not send prompt for already-approved chat
            channel._maybe_send_sender_approval_prompt(
                chat_id="1234",
                chat={"id": 1234, "type": "private", "username": "alice"},
                message={"text": "hello"},
            )

            # No HTTP call should be made
            assert not mock_http.called

        channel.close()

    def test_denied_sender_no_prompt(self) -> None:
        """Explicitly denied sender → no approval prompt."""
        instance = _make_instance()
        conn = queue.connect(instance)  # Uses same DB initialization

        # Insert denied chat
        chats_module.upsert_chat(
            conn=conn,
            channel="telegram",
            chat_id="1234",
            chat_type="private",
            title="eve",
            auth_status="denied",
        )
        conn.commit()

        from gateway.channels.telegram import TelegramChannel
        from gateway.config import ChannelConfig

        cfg = ChannelConfig(
            enabled=True,
            chat_ids=["9999"],
            token_env="TELEGRAM_BOT_TOKEN",
        )

        with patch("gateway.channels.telegram.http_json") as mock_http:
            channel = TelegramChannel(
                instance_dir=instance,
                cfg=cfg,
                log=MagicMock(),
            )

            # Should not send prompt for denied chat
            channel._maybe_send_sender_approval_prompt(
                chat_id="1234",
                chat={"id": 1234, "type": "private", "username": "eve"},
                message={"text": "hello"},
            )

            # No HTTP call
            assert not mock_http.called

        channel.close()

    def test_duplicate_prompt_suppressed(self) -> None:
        """Within same process, second prompt suppressed."""
        instance = _make_instance()

        from gateway.channels.telegram import TelegramChannel
        from gateway.config import ChannelConfig

        cfg = ChannelConfig(
            enabled=True,
            chat_ids=["9999"],
            token_env="TELEGRAM_BOT_TOKEN",
        )

        with patch("gateway.channels.telegram.http_json") as mock_http:
            mock_http.return_value = {"ok": True, "result": {"message_id": 999}}

            channel = TelegramChannel(
                instance_dir=instance,
                cfg=cfg,
                log=MagicMock(),
            )

            channel._record_chat(
                chat={"id": 5555, "type": "private", "username": "bob"},
                message={"message_id": 100, "text": "first"},
            )

            # First prompt
            channel._maybe_send_sender_approval_prompt(
                chat_id="5555",
                chat={"id": 5555, "type": "private", "username": "bob"},
                message={"text": "first"},
            )
            assert mock_http.call_count == 1

            # Second prompt from same sender (same process)
            channel._maybe_send_sender_approval_prompt(
                chat_id="5555",
                chat={"id": 5555, "type": "private", "username": "bob"},
                message={"text": "second"},
            )
            # Should not increment HTTP call count
            assert mock_http.call_count == 1

        channel.close()

    def test_message_preview_in_prompt(self) -> None:
        """Message preview included in approval prompt."""
        instance = _make_instance()

        from gateway.channels.telegram import TelegramChannel
        from gateway.config import ChannelConfig

        cfg = ChannelConfig(
            enabled=True,
            chat_ids=["9999"],
            token_env="TELEGRAM_BOT_TOKEN",
        )

        with patch("gateway.channels.telegram.http_json") as mock_http:
            mock_http.return_value = {"ok": True, "result": {"message_id": 999}}

            channel = TelegramChannel(
                instance_dir=instance,
                cfg=cfg,
                log=MagicMock(),
            )

            channel._record_chat(
                chat={"id": 6666, "type": "private"},
                message={"message_id": 100, "text": "important business idea"},
            )

            channel._maybe_send_sender_approval_prompt(
                chat_id="6666",
                chat={"id": 6666, "type": "private"},
                message={"text": "important business idea"},
            )

            # Check prompt payload
            call_args = mock_http.call_args
            payload = call_args.kwargs["data"]
            text = payload["text"]
            # Preview should be included (will be escaped for MarkdownV2)
            assert "important business idea" in text or "important" in text

        channel.close()


if __name__ == "__main__":
    unittest.main()
