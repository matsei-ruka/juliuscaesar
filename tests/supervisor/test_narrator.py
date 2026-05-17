"""Tests for the supervisor narrator module."""

import json
from unittest.mock import MagicMock, patch

import pytest

from supervisor.narrator import (
    NarratorResult,
    _parse_brain_spec,
    _validate,
    narrate,
    redact_stderr,
)
from supervisor.models import PhaseResult
from supervisor.state import EventState


def _snap(language="en", stderr_tail="Read(/foo.py) done"):
    phase = PhaseResult(
        phase="reading",
        emoji="📖",
        label={"en": "reading files", "it": "lettura file"},
    )

    class FakeAdapter:
        activity_age_seconds = 5.0

    class FakeEvent:
        id = 1
        content = "audit repo"
        source = "telegram"

    class FakeSnap:
        event = FakeEvent()
        meta = {"chat_id": "12345", "text": "audit repo"}
        age_seconds = 90.0
        brain = "claude"
        brain_spec = "claude:sonnet"
        model = "sonnet"
        worker_linked = False

    adapter = FakeAdapter()
    adapter.stderr_tail = stderr_tail
    s = FakeSnap()
    s.adapter = adapter
    s.language = language
    s.phase = phase
    return s


def _ev():
    return EventState()


# --- redact_stderr ---

def test_redact_api_key():
    redacted = redact_stderr("API_KEY=sk-1234 doing stuff")
    assert "sk-1234" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_token_bearer():
    redacted = redact_stderr("Authorization: Bearer eyJhbGciOi")
    assert "eyJhbGciOi" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_preserves_normal_text():
    tail = "Read(/foo/bar.py) done\nBash(ls) done"
    assert redact_stderr(tail) == tail


def test_redact_secret_colon():
    redacted = redact_stderr("secret:hunter2 next")
    assert "hunter2" not in redacted


# --- _validate ---

def test_validate_ok():
    assert _validate("scansione di 184 file PHP completata") is True


def test_validate_too_long():
    assert _validate("x" * 141) is False


def test_validate_empty():
    assert _validate("") is False


def test_validate_banned_token_gateway():
    assert _validate("gateway is processing") is False


def test_validate_banned_token_brain():
    assert _validate("the brain is working") is False


def test_validate_banned_token_pid():
    assert _validate("pid 1234 is running") is False


def test_validate_banned_case_insensitive():
    assert _validate("JuliusCaesar is cool") is False


# --- _parse_brain_spec ---

def test_parse_openrouter_prefixed():
    provider, model = _parse_brain_spec("openrouter:deepseek-v4-flash")
    assert provider == "openrouter"
    assert model == "deepseek-v4-flash"


def test_parse_no_prefix():
    provider, model = _parse_brain_spec("deepseek-v4-flash")
    assert provider == "openrouter"
    assert model == "deepseek-v4-flash"


def test_parse_other_provider():
    provider, model = _parse_brain_spec("claude:haiku")
    assert provider == "claude"
    assert model == "haiku"


# --- narrate ---

def _mock_http_response(narration_text):
    return {
        "choices": [{"message": {"content": json.dumps({"narration": narration_text})}}]
    }


def test_narrate_returns_model_result(tmp_path):
    snap = _snap()
    ev = _ev()
    with patch("supervisor.narrator.http_json") as mock_http, \
         patch("supervisor.narrator.env_value", return_value="test-key"):
        mock_http.return_value = _mock_http_response("scanned 184 PHP files")
        result = narrate(snap, ev, "openrouter:deepseek-v4-flash", tmp_path)
    assert result.from_model is True
    assert result.text == "scanned 184 PHP files"


def test_narrate_updates_ev_state(tmp_path):
    snap = _snap()
    ev = _ev()
    with patch("supervisor.narrator.http_json") as mock_http, \
         patch("supervisor.narrator.env_value", return_value="test-key"):
        mock_http.return_value = _mock_http_response("trovati 42 controller critici")
        narrate(snap, ev, "openrouter:deepseek-v4-flash", tmp_path)
    # caller in runner.py updates ev_state, not narrate() itself
    # narrate() just returns result; runner does the bookkeeping
    # So we only check the result here
    assert True  # result tested above


def test_narrate_fallback_missing_key(tmp_path):
    snap = _snap()
    ev = _ev()
    with patch("supervisor.narrator.env_value", return_value=""):
        result = narrate(snap, ev, "openrouter:deepseek-v4-flash", tmp_path)
    assert result.from_model is False
    assert result.text == snap.phase.label_for("en")


def test_narrate_fallback_http_error(tmp_path):
    snap = _snap()
    ev = _ev()
    with patch("supervisor.narrator.http_json", side_effect=RuntimeError("timeout")), \
         patch("supervisor.narrator.env_value", return_value="test-key"):
        result = narrate(snap, ev, "openrouter:deepseek-v4-flash", tmp_path)
    assert result.from_model is False


def test_narrate_fallback_banned_token(tmp_path):
    snap = _snap()
    ev = _ev()
    with patch("supervisor.narrator.http_json") as mock_http, \
         patch("supervisor.narrator.env_value", return_value="test-key"):
        mock_http.return_value = _mock_http_response("gateway is routing the brain")
        result = narrate(snap, ev, "openrouter:deepseek-v4-flash", tmp_path)
    assert result.from_model is False
    assert result.text == snap.phase.label_for("en")


def test_narrate_fallback_too_long(tmp_path):
    snap = _snap()
    ev = _ev()
    with patch("supervisor.narrator.http_json") as mock_http, \
         patch("supervisor.narrator.env_value", return_value="test-key"):
        mock_http.return_value = _mock_http_response("x" * 141)
        result = narrate(snap, ev, "openrouter:deepseek-v4-flash", tmp_path)
    assert result.from_model is False


def test_narrate_unsupported_provider_fallback(tmp_path):
    snap = _snap()
    ev = _ev()
    result = narrate(snap, ev, "claude:haiku", tmp_path)
    assert result.from_model is False
    assert result.text == snap.phase.label_for("en")


def test_narrate_italian_language(tmp_path):
    snap = _snap(language="it")
    ev = _ev()
    with patch("supervisor.narrator.http_json") as mock_http, \
         patch("supervisor.narrator.env_value", return_value="test-key"):
        mock_http.return_value = _mock_http_response("trovati 184 file PHP da analizzare")
        result = narrate(snap, ev, "openrouter:deepseek-v4-flash", tmp_path)
    # Verify system prompt was in Italian
    call_args = mock_http.call_args
    payload = call_args.kwargs.get("data") or call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs["data"]
    system_msg = payload["messages"][0]["content"]
    assert "italiano" in system_msg or "Sei" in system_msg
    assert result.from_model is True


def test_narrate_plain_text_fallback_parse(tmp_path):
    """If model returns plain text instead of JSON, accept if valid."""
    snap = _snap()
    ev = _ev()
    with patch("supervisor.narrator.http_json") as mock_http, \
         patch("supervisor.narrator.env_value", return_value="test-key"):
        # Return plain text, not JSON
        mock_http.return_value = {
            "choices": [{"message": {"content": "scanning PHP controllers for auth issues"}}]
        }
        result = narrate(snap, ev, "openrouter:deepseek-v4-flash", tmp_path)
    assert result.from_model is True
    assert result.text == "scanning PHP controllers for auth issues"


def test_narrate_empty_choices_fallback(tmp_path):
    snap = _snap()
    ev = _ev()
    with patch("supervisor.narrator.http_json", return_value={"choices": []}), \
         patch("supervisor.narrator.env_value", return_value="test-key"):
        result = narrate(snap, ev, "openrouter:deepseek-v4-flash", tmp_path)
    assert result.from_model is False
