"""Tests for normalize_sender_addr — RFC 5322 mailbox extraction."""

from __future__ import annotations

import pytest

from lib.channels.email.normalize import normalize_sender_addr


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("sergio@scovai.com", "sergio@scovai.com"),
        ("Sergio Gutierrez <sergio@scovai.com>", "sergio@scovai.com"),
        ('"S, G" <a@b>', "a@b"),
        ("a@b (comment)", "a@b"),
        ("UPPER@EXAMPLE.COM", "upper@example.com"),
        # None / empty / invalid
        ("", None),
        (None, None),
        ("no-at", None),
        ("not an email", None),
        ("   ", None),
    ],
)
def test_normalize_sender_addr(raw: str | None, expected: str | None) -> None:
    assert normalize_sender_addr(raw) == expected
