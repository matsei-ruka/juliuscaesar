"""Tests for SMTPClient From header — display-name and bare-address forms."""

from __future__ import annotations

import email.utils

from lib.channels.email.smtp_client import SMTPClient


def _client(from_display_name: str | None = None) -> SMTPClient:
    return SMTPClient(
        host="mail.example.com",
        user="daniel.mercer@omnisage.org",
        password="x",
        from_display_name=from_display_name,
    )


def test_from_header_with_display_name() -> None:
    client = _client("Daniel Mercer")
    msg = client._build_message(
        to=["recipient@example.com"],
        cc=[],
        subject="Test",
        body="Hello",
        in_reply_to=None,
        references=None,
        signature="",
    )
    addresses = email.utils.getaddresses([msg["From"]])
    assert len(addresses) == 1
    name, addr = addresses[0]
    assert name == "Daniel Mercer"
    assert addr == "daniel.mercer@omnisage.org"


def test_from_header_with_comma_in_display_name() -> None:
    client = _client("Mercer, Daniel")
    msg = client._build_message(
        to=["recipient@example.com"],
        cc=[],
        subject="Test",
        body="Hello",
        in_reply_to=None,
        references=None,
        signature="",
    )
    addresses = email.utils.getaddresses([msg["From"]])
    assert len(addresses) == 1
    name, addr = addresses[0]
    assert name == "Mercer, Daniel"
    assert addr == "daniel.mercer@omnisage.org"


def test_from_header_without_display_name() -> None:
    client = _client(None)
    msg = client._build_message(
        to=["recipient@example.com"],
        cc=[],
        subject="Test",
        body="Hello",
        in_reply_to=None,
        references=None,
        signature="",
    )
    assert msg["From"] == "daniel.mercer@omnisage.org"
