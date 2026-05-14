"""DKIM soft-import behavior."""

from __future__ import annotations

import email
from email.message import EmailMessage

from approvals import dkim as dkim_helper


def test_dkim_available_does_not_raise() -> None:
    # Soft-import contract: function exists, returns bool, no exception.
    assert dkim_helper.dkim_available() in (True, False)


def test_verify_message_without_dkim_returns_false() -> None:
    if dkim_helper.dkim_available():
        return  # skip on hosts where dkimpy is installed
    passed, reason = dkim_helper.verify_message(b"From: x\n\nbody")
    assert passed is False
    assert "not_installed" in reason


def test_authentication_results_trusts_named_mta() -> None:
    msg = EmailMessage()
    msg["From"] = "op@example.com"
    msg["Authentication-Results"] = "mx.example.com; dkim=pass header.d=example.com"
    parsed = email.message_from_bytes(msg.as_bytes())
    assert dkim_helper.authentication_results_pass(parsed, ("mx.example.com",))
    assert not dkim_helper.authentication_results_pass(parsed, ())


def test_signing_domain_extract() -> None:
    msg = EmailMessage()
    msg["From"] = "op@example.com"
    msg["DKIM-Signature"] = "v=1; a=rsa-sha256; d=example.com; s=k1; b=abc"
    parsed = email.message_from_bytes(msg.as_bytes())
    assert dkim_helper.signing_domain(parsed) == "example.com"
