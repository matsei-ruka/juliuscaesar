"""Email decide path with stubbed DKIM gate."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from approvals.channels.email import parse_decide_line, render_body, verify_inbound
from approvals.service import raise_


def test_parse_decide_line_strict() -> None:
    aid = "a" * 32
    tok = "b" * 64
    assert parse_decide_line(f"APPROVE {aid} {tok}") == ("approve", aid, tok)
    assert parse_decide_line(f"reject {aid} {tok}") == ("reject", aid, tok)
    assert parse_decide_line("APPROVE shortid token") is None
    assert parse_decide_line("") is None


def test_render_body_includes_approval_id(instance_dir: Path) -> None:
    rec = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload={"description": "y"},
        producer="test",
        notify_telegram=False,
    )
    body = render_body(rec)
    assert rec.approval_id in body
    assert rec.callback_token in body
    assert "APPROVE" in body


def _trusted_principal(instance_dir: Path) -> None:
    (instance_dir / "ops" / "gateway.yaml").write_text(
        "principal:\n  email: op@example.com\n  email_domain: example.com\n",
        encoding="utf-8",
    )
    (instance_dir / "ops" / "approvals.yaml").write_text(
        "dkim:\n  trusted_mta_hostnames:\n    - mx.example.com\n",
        encoding="utf-8",
    )


def _build_email(rec, action: str, sender: str = "op@example.com") -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "jc@example.com"
    msg["Subject"] = "decision"
    msg["Authentication-Results"] = (
        f"mx.example.com; dkim=pass header.d=example.com"
    )
    msg.set_content(f"{action.upper()} {rec.approval_id} {rec.callback_token}\n")
    return msg.as_bytes()


def test_verify_inbound_approve(instance_dir: Path) -> None:
    _trusted_principal(instance_dir)
    rec = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload={"description": "y"},
        producer="test",
        notify_telegram=False,
        notify_email=False,
    )
    raw = _build_email(rec, "approve")
    result = verify_inbound(instance_dir, raw_message=raw)
    assert result["ok"] is True
    assert result["action"] == "approve"
    assert result["approval"].status == "approved"


def test_verify_inbound_rejects_wrong_sender(instance_dir: Path) -> None:
    _trusted_principal(instance_dir)
    rec = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload={"description": "y"},
        producer="test",
        notify_telegram=False,
    )
    raw = _build_email(rec, "approve", sender="evil@example.com")
    result = verify_inbound(instance_dir, raw_message=raw)
    assert result["ok"] is False
    assert result["error"] == "from_mismatch"


def test_verify_inbound_requires_dkim_when_no_trusted_mta(instance_dir: Path) -> None:
    (instance_dir / "ops" / "gateway.yaml").write_text(
        "principal:\n  email: op@example.com\n  email_domain: example.com\n",
        encoding="utf-8",
    )
    rec = raise_(
        instance_dir,
        kind="action",
        title="x",
        payload={"description": "y"},
        producer="test",
        notify_telegram=False,
    )
    raw = _build_email(rec, "approve")
    with mock.patch("approvals.channels.email.dkim_helper.dkim_available", return_value=False):
        result = verify_inbound(instance_dir, raw_message=raw)
    assert result["ok"] is False
    assert result["error"] == "dkim_unavailable"
