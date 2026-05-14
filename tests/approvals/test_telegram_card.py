"""Telegram card rendering + callback parsing + decide gate."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from approvals.channels.telegram import (
    CALLBACK_PREFIX,
    handle_callback_query,
    inline_keyboard,
    parse_callback_data,
    render_card,
)
from approvals.service import raise_


def _record(instance_dir: Path):
    return raise_(
        instance_dir,
        kind="action",
        title="hello",
        body="some body",
        payload={"description": "x"},
        producer="test",
        notify_telegram=False,
        expires_in=timedelta(hours=1),
    )


def test_render_card_contains_title_and_id(instance_dir: Path) -> None:
    rec = _record(instance_dir)
    text = render_card(rec)
    assert "hello" in text
    assert rec.short_id in text
    assert "action" in text


def test_inline_keyboard_callback_data(instance_dir: Path) -> None:
    rec = _record(instance_dir)
    kb = inline_keyboard(rec)
    buttons = kb["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == f"{CALLBACK_PREFIX}{rec.approval_id}:approve"
    assert buttons[1]["callback_data"] == f"{CALLBACK_PREFIX}{rec.approval_id}:reject"
    # Telegram cap is 64 bytes — check we stay within it.
    for b in buttons:
        assert len(b["callback_data"].encode()) <= 64


def test_parse_callback_data_roundtrip() -> None:
    assert parse_callback_data("apv:abc123:approve") == ("abc123", "approve")
    assert parse_callback_data("apv:abc123:reject") == ("abc123", "reject")
    assert parse_callback_data("garbage") is None
    assert parse_callback_data("apv:only") is None
    assert parse_callback_data("apv:abc:weird") is None


def test_handle_callback_rejects_non_principal(instance_dir: Path) -> None:
    (instance_dir / "ops" / "gateway.yaml").write_text(
        "principal:\n  telegram_chat_id: 100\n  telegram_user_id: 100\n",
        encoding="utf-8",
    )
    rec = _record(instance_dir)
    result = handle_callback_query(
        instance_dir,
        callback_data=f"apv:{rec.approval_id}:approve",
        from_user_id="999",
    )
    assert result["ok"] is False
    assert result["error"] == "not_authorized"


def test_handle_callback_accepts_principal(instance_dir: Path) -> None:
    (instance_dir / "ops" / "gateway.yaml").write_text(
        "principal:\n  telegram_chat_id: 100\n  telegram_user_id: 100\n",
        encoding="utf-8",
    )
    rec = _record(instance_dir)
    result = handle_callback_query(
        instance_dir,
        callback_data=f"apv:{rec.approval_id}:approve",
        from_user_id="100",
    )
    assert result["ok"] is True
    assert result["action"] == "approve"
    assert result["approval"].status == "approved"
