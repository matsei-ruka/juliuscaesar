"""Tests for gateway EmailChannel outbound behavior."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from gateway.config import ChannelConfig
from gateway.channels.email import EmailChannel
from gateway.channels import email_dispatcher


class _Adapter:
    def __init__(self):
        self.sent = []

    def send_reply(self, **kwargs):
        self.sent.append(kwargs)
        return "<sent@example.com>"


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


def test_sender_promotion_to_trusted_is_resolved_at_send_time() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        instance = Path(tmp)
        (instance / "ops").mkdir(parents=True)
        (instance / "ops" / "gateway.yaml").write_text(
            """
channels:
  email:
    senders:
      trusted: [client@example.com]
      external: []
      blocklist: []
""",
            encoding="utf-8",
        )
        adapter = _Adapter()
        channel = EmailChannel(instance, ChannelConfig(enabled=True), log=lambda _msg: None)
        meta = {
            "email_to": "client@example.com",
            "email_subject": "Question",
            "email_message_id": "<1@example.com>",
            "email_uid": "42",
            "sender_tier": "external",
        }
        with patch.object(channel, "_build_adapter", return_value=adapter):
            result = channel.send("Direct response", meta)
        assert result == "<sent@example.com>"
        assert adapter.sent[0]["recipient"] == "client@example.com"
        draft_path = (
            email_dispatcher.drafts_dir(instance)
            / email_dispatcher._sender_key("client@example.com")
            / "draft_42.json"
        )
        assert not draft_path.exists()


def test_sender_removed_from_trusted_defaults_back_to_external_at_send_time() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        instance = Path(tmp)
        (instance / "ops").mkdir(parents=True)
        (instance / "ops" / "gateway.yaml").write_text(
            """
channels:
  email:
    senders:
      trusted: []
      external: []
      blocklist: []
""",
            encoding="utf-8",
        )
        channel = EmailChannel(instance, ChannelConfig(enabled=True), log=lambda _msg: None)
        meta = {
            "email_to": "client@example.com",
            "email_subject": "Question",
            "email_message_id": "<1@example.com>",
            "email_uid": "42",
            "sender_tier": "trusted",
        }
        with patch.object(channel, "_build_adapter") as build_adapter:
            result = channel.send("Draft response", meta)
        assert result == "draft:draft_42"
        build_adapter.assert_not_called()


def test_blocked_sender_is_not_sent_even_if_meta_says_trusted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        instance = Path(tmp)
        (instance / "ops").mkdir(parents=True)
        (instance / "ops" / "gateway.yaml").write_text(
            """
channels:
  email:
    senders:
      trusted: []
      external: []
      blocklist: [client@example.com]
""",
            encoding="utf-8",
        )
        adapter = _Adapter()
        channel = EmailChannel(instance, ChannelConfig(enabled=True), log=lambda _msg: None)
        meta = {
            "email_to": "client@example.com",
            "email_subject": "Question",
            "email_message_id": "<1@example.com>",
            "email_uid": "42",
            "sender_tier": "trusted",
        }
        with patch.object(channel, "_build_adapter", return_value=adapter) as build_adapter:
            result = channel.send("Do not send", meta)
        assert result is None
        assert adapter.sent == []
        build_adapter.assert_not_called()
