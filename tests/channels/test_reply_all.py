"""Tests for EmailChannelAdapter reply-all CC construction."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.channels.email.adapter import EmailChannelAdapter


def _adapter(reply_all: bool = True, imap_user: str = "agent@example.com") -> EmailChannelAdapter:
    with tempfile.TemporaryDirectory() as tmp:
        instance = Path(tmp)
        (instance / "ops").mkdir()
        (instance / "ops" / "gateway.yaml").write_text(
            "channels:\n  email:\n    senders:\n      trusted: []\n",
            encoding="utf-8",
        )
        config = {
            "smtp": {"reply_all": reply_all},
            "imap": {"host": "mail.example.com"},
        }
        env = {
            "IMAP_HOST": "mail.example.com",
            "IMAP_USER": imap_user,
            "IMAP_PASSWORD": "x",
            "SMTP_PORT": "587",
            "IMAP_PORT": "993",
        }
        with patch("lib.channels.email.adapter.IMAPClient"), \
             patch("lib.channels.email.adapter.SMTPClient"), \
             patch("lib.channels.email.adapter.SenderAuthorizer"):
            adapter = EmailChannelAdapter(instance_dir=instance, config=config, env=env)
            adapter.imap_user = imap_user
            adapter.reply_all = reply_all
            return adapter


class TestBuildReplyCc:
    def test_reply_all_excludes_self_and_to(self) -> None:
        adapter = _adapter(reply_all=True, imap_user="agent@example.com")
        cc = adapter._build_reply_cc(
            recipient="alice@example.com",
            original_to=["agent@example.com", "alice@example.com", "bob@example.com"],
            original_cc=["carol@example.com"],
        )
        addrs = {a.lower() for a in cc}
        assert "bob@example.com" in addrs
        assert "carol@example.com" in addrs
        assert "agent@example.com" not in addrs
        assert "alice@example.com" not in addrs

    def test_reply_all_false_returns_empty(self) -> None:
        adapter = _adapter(reply_all=False)
        cc = adapter._build_reply_cc(
            recipient="alice@example.com",
            original_to=["bob@example.com"],
            original_cc=["carol@example.com"],
        )
        assert cc == []

    def test_self_excluded_case_insensitively(self) -> None:
        adapter = _adapter(reply_all=True, imap_user="Agent@Example.COM")
        adapter.imap_user = "Agent@Example.COM"
        cc = adapter._build_reply_cc(
            recipient="alice@example.com",
            original_to=["agent@example.com"],
            original_cc=["bob@example.com"],
        )
        addrs = {a.lower() for a in cc}
        assert "agent@example.com" not in addrs
        assert "bob@example.com" in addrs

    def test_no_original_recipients_returns_empty(self) -> None:
        adapter = _adapter(reply_all=True)
        cc = adapter._build_reply_cc(
            recipient="alice@example.com",
            original_to=None,
            original_cc=None,
        )
        assert cc == []

    def test_display_name_recipients_deduplicated(self) -> None:
        adapter = _adapter(reply_all=True, imap_user="agent@example.com")
        cc = adapter._build_reply_cc(
            recipient="alice@example.com",
            original_to=["Bob Smith <bob@example.com>"],
            original_cc=["bob@example.com"],  # same address, different form
        )
        # Should appear once, not twice
        assert len(cc) == 1
