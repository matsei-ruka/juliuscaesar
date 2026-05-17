"""Tests for supervisor Slack + Discord delivery functions (Phase 4)."""

from pathlib import Path
from unittest.mock import patch

import pytest

from supervisor.cards import Card
from supervisor.delivery import (
    edit_card_discord,
    edit_card_slack,
    send_card_discord,
    send_card_slack,
)


def _card(text="🛠️ test task\n\nPhase: coding"):
    return Card(text=text, phase="coding", emoji="🛠️", language="en")


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def test_send_card_slack_returns_ts(tmp_path):
    with patch("supervisor.delivery.http_json") as mock_http, \
         patch("supervisor.delivery.env_value", return_value="xoxb-test"):
        mock_http.return_value = {"ok": True, "ts": "1715984400.123456", "channel": "C123"}
        ts = send_card_slack(instance_dir=tmp_path, channel="C123", card=_card())
    assert ts == "1715984400.123456"


def test_send_card_slack_missing_token_returns_none(tmp_path):
    with patch("supervisor.delivery.env_value", return_value=""):
        result = send_card_slack(instance_dir=tmp_path, channel="C123", card=_card())
    assert result is None


def test_send_card_slack_api_failure_returns_none(tmp_path):
    with patch("supervisor.delivery.http_json") as mock_http, \
         patch("supervisor.delivery.env_value", return_value="xoxb-test"):
        mock_http.return_value = {"ok": False, "error": "channel_not_found"}
        result = send_card_slack(instance_dir=tmp_path, channel="C123", card=_card())
    assert result is None


def test_send_card_slack_http_error_returns_none(tmp_path):
    with patch("supervisor.delivery.http_json", side_effect=RuntimeError("timeout")), \
         patch("supervisor.delivery.env_value", return_value="xoxb-test"):
        result = send_card_slack(instance_dir=tmp_path, channel="C123", card=_card())
    assert result is None


def test_send_card_slack_missing_channel_returns_none(tmp_path):
    with patch("supervisor.delivery.env_value", return_value="xoxb-test"):
        result = send_card_slack(instance_dir=tmp_path, channel="", card=_card())
    assert result is None


def test_send_card_slack_passes_thread_ts(tmp_path):
    with patch("supervisor.delivery.http_json") as mock_http, \
         patch("supervisor.delivery.env_value", return_value="xoxb-test"):
        mock_http.return_value = {"ok": True, "ts": "999.111"}
        send_card_slack(
            instance_dir=tmp_path, channel="C123", card=_card(), thread_ts="888.000"
        )
    payload = mock_http.call_args.kwargs["data"]
    assert payload.get("thread_ts") == "888.000"


def test_edit_card_slack_returns_true(tmp_path):
    with patch("supervisor.delivery.http_json") as mock_http, \
         patch("supervisor.delivery.env_value", return_value="xoxb-test"):
        mock_http.return_value = {"ok": True, "ts": "1715984400.123456"}
        ok = edit_card_slack(
            instance_dir=tmp_path, channel="C123", ts="1715984400.123456", card=_card()
        )
    assert ok is True


def test_edit_card_slack_not_modified_treated_as_ok(tmp_path):
    with patch("supervisor.delivery.http_json") as mock_http, \
         patch("supervisor.delivery.env_value", return_value="xoxb-test"):
        mock_http.return_value = {"ok": False, "error": "not_modified"}
        ok = edit_card_slack(
            instance_dir=tmp_path, channel="C123", ts="1715984400.123456", card=_card()
        )
    assert ok is True


def test_edit_card_slack_message_not_found_returns_false(tmp_path):
    """Bug #8 — message_not_found means the card is GONE; previous behavior
    treated it as success which kept the dead message_id in state forever.
    Returning False lets the runner clear the id and re-send next tick.
    """
    with patch("supervisor.delivery.http_json") as mock_http, \
         patch("supervisor.delivery.env_value", return_value="xoxb-test"):
        mock_http.return_value = {"ok": False, "error": "message_not_found"}
        ok = edit_card_slack(
            instance_dir=tmp_path, channel="C123", ts="1715984400.123456", card=_card()
        )
    assert ok is False


def test_edit_card_slack_api_failure_returns_false(tmp_path):
    with patch("supervisor.delivery.http_json") as mock_http, \
         patch("supervisor.delivery.env_value", return_value="xoxb-test"):
        mock_http.return_value = {"ok": False, "error": "invalid_auth"}
        ok = edit_card_slack(
            instance_dir=tmp_path, channel="C123", ts="1715984400.123456", card=_card()
        )
    assert ok is False


def test_edit_card_slack_missing_token_returns_false(tmp_path):
    with patch("supervisor.delivery.env_value", return_value=""):
        ok = edit_card_slack(
            instance_dir=tmp_path, channel="C123", ts="1715984400.123456", card=_card()
        )
    assert ok is False


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def test_send_card_discord_returns_message_id(tmp_path):
    with patch("supervisor.delivery.http_json") as mock_http, \
         patch("supervisor.delivery.env_value", return_value="discord-bot-token"):
        mock_http.return_value = {"id": "1234567890", "content": "test"}
        mid = send_card_discord(
            instance_dir=tmp_path, channel_id="987654321", card=_card()
        )
    assert mid == "1234567890"


def test_send_card_discord_missing_token_returns_none(tmp_path):
    with patch("supervisor.delivery.env_value", return_value=""):
        result = send_card_discord(
            instance_dir=tmp_path, channel_id="987654321", card=_card()
        )
    assert result is None


def test_send_card_discord_http_error_returns_none(tmp_path):
    with patch("supervisor.delivery.http_json", side_effect=RuntimeError("timeout")), \
         patch("supervisor.delivery.env_value", return_value="discord-bot-token"):
        result = send_card_discord(
            instance_dir=tmp_path, channel_id="987654321", card=_card()
        )
    assert result is None


def test_send_card_discord_truncates_to_2000(tmp_path):
    long_card = Card(text="x" * 2500, phase="coding", emoji="🛠️", language="en")
    with patch("supervisor.delivery.http_json") as mock_http, \
         patch("supervisor.delivery.env_value", return_value="discord-bot-token"):
        mock_http.return_value = {"id": "1"}
        send_card_discord(instance_dir=tmp_path, channel_id="987654321", card=long_card)
    payload = mock_http.call_args.kwargs["data"]
    assert len(payload["content"]) == 2000


def test_send_card_discord_uses_bot_auth_header(tmp_path):
    with patch("supervisor.delivery.http_json") as mock_http, \
         patch("supervisor.delivery.env_value", return_value="my-discord-token"):
        mock_http.return_value = {"id": "1"}
        send_card_discord(instance_dir=tmp_path, channel_id="987654321", card=_card())
    extra_headers = mock_http.call_args.kwargs.get("extra_headers") or {}
    assert extra_headers.get("Authorization") == "Bot my-discord-token"


def test_edit_card_discord_returns_true(tmp_path):
    with patch("supervisor.delivery.http_json") as mock_http, \
         patch("supervisor.delivery.env_value", return_value="discord-bot-token"):
        mock_http.return_value = {"id": "1234567890", "content": "updated"}
        ok = edit_card_discord(
            instance_dir=tmp_path,
            channel_id="987654321",
            message_id="1234567890",
            card=_card(),
        )
    assert ok is True


def test_edit_card_discord_uses_patch_method(tmp_path):
    with patch("supervisor.delivery.http_json") as mock_http, \
         patch("supervisor.delivery.env_value", return_value="discord-bot-token"):
        mock_http.return_value = {"id": "1"}
        edit_card_discord(
            instance_dir=tmp_path,
            channel_id="987654321",
            message_id="1234567890",
            card=_card(),
        )
    assert mock_http.call_args.kwargs.get("method") == "PATCH"


def test_edit_card_discord_http_error_returns_false(tmp_path):
    with patch("supervisor.delivery.http_json", side_effect=RuntimeError("conn error")), \
         patch("supervisor.delivery.env_value", return_value="discord-bot-token"):
        ok = edit_card_discord(
            instance_dir=tmp_path,
            channel_id="987654321",
            message_id="1234567890",
            card=_card(),
        )
    assert ok is False


def test_edit_card_discord_missing_id_in_response_returns_false(tmp_path):
    with patch("supervisor.delivery.http_json") as mock_http, \
         patch("supervisor.delivery.env_value", return_value="discord-bot-token"):
        mock_http.return_value = {"error": "Unknown Message"}
        ok = edit_card_discord(
            instance_dir=tmp_path,
            channel_id="987654321",
            message_id="1234567890",
            card=_card(),
        )
    assert ok is False
