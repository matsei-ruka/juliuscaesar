"""Tests for gateway EmailChannel outbound behavior."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from gateway.config import ChannelConfig
from gateway.channels.email import EmailChannel
from gateway.channels import email_dispatcher


def test_external_sender_reply_is_drafted_not_sent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        instance = Path(tmp)
        channel = EmailChannel(instance, ChannelConfig(enabled=True), log=lambda _msg: None)
        meta = {
            "email_to": "client@example.com",
            "email_subject": "Question",
            "email_message_id": "<1@example.com>",
            "email_uid": "42",
            "sender_tier": "external",
        }
        with patch.object(channel, "_build_adapter") as build_adapter:
            result = channel.send("Draft response", meta)
        assert result == "draft:draft_42"
        build_adapter.assert_not_called()
        draft_path = (
            email_dispatcher.drafts_dir(instance)
            / email_dispatcher._sender_key("client@example.com")
            / "draft_42.json"
        )
        assert draft_path.exists()
