"""Tests for three-tier email sender authorization."""

from __future__ import annotations

import tempfile
from pathlib import Path

from lib.channels.email.authorization import SenderAuthorizer


def _write_config(instance: Path, body: str) -> Path:
    path = instance / "ops" / "gateway.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_three_tier_sender_policy_and_blocklist_precedence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _write_config(
            Path(tmp),
            """
channels:
  email:
    senders:
      trusted: [trusted@example.com, both@example.com]
      external: [client@example.com]
      blocklist: [blocked@example.com, both@example.com]
""",
        )
        auth = SenderAuthorizer(cfg, check_interval=0)
        assert auth.check("trusted@example.com") == "trusted"
        assert auth.check("client@example.com") == "external"
        assert auth.check("blocked@example.com") == "blocked"
        assert auth.check("both@example.com") == "blocked"
        assert auth.check("new@example.com") == "external"
        assert auth.check("") == "blocked"
        assert auth.check("not-an-email") == "blocked"


def test_legacy_allowed_is_treated_as_trusted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _write_config(
            Path(tmp),
            """
channels:
  email:
    senders:
      allowed: [legacy@example.com]
""",
        )
        auth = SenderAuthorizer(cfg, check_interval=0)
        assert auth.check("legacy@example.com") == "trusted"


def test_sender_lists_are_trimmed_and_invalid_updates_are_ignored() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        instance = Path(tmp)
        cfg = _write_config(
            instance,
            """
channels:
  email:
    senders:
      trusted: [" trusted@example.com "]
      external: ["", "not-an-email"]
      blocklist: []
""",
        )
        auth = SenderAuthorizer(cfg, check_interval=0)
        assert auth.check("trusted@example.com") == "trusted"
        assert auth.check("not-an-email") == "blocked"
