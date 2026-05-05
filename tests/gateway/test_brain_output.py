"""Unit tests for the structured brain-output contract parser."""

from __future__ import annotations

from gateway.brain_output import BrainOutput, parse_brain_output


def test_empty_returns_silent_no_error():
    out = parse_brain_output("")
    assert out == BrainOutput(push_message_sent=False, message="", parse_error=None)


def test_none_returns_silent_no_error():
    out = parse_brain_output(None)
    assert out.push_message_sent is False
    assert out.message == ""
    assert out.parse_error is None


def test_canonical_push_handled():
    raw = '{"push_message_sent": true, "message": "Sent report 994."}'
    out = parse_brain_output(raw)
    assert out.push_message_sent is True
    assert out.message == "Sent report 994."
    assert out.parse_error is None


def test_canonical_relay():
    raw = '{"push_message_sent": false, "message": "Hello user."}'
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == "Hello user."
    assert out.parse_error is None


def test_strips_json_code_fence():
    raw = '```json\n{"push_message_sent": false, "message": "ok"}\n```'
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == "ok"
    assert out.parse_error is None


def test_strips_bare_code_fence():
    raw = '```\n{"push_message_sent": true, "message": "x"}\n```'
    out = parse_brain_output(raw)
    assert out.push_message_sent is True
    assert out.message == "x"


def test_invalid_json_falls_back_to_raw_with_error():
    raw = "Sent. Message ID 447. SILENT"
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == raw
    assert out.parse_error is not None
    assert "JSONDecodeError" in out.parse_error


def test_non_object_falls_back():
    raw = '["not", "an", "object"]'
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == raw
    assert out.parse_error is not None
    assert "expected JSON object" in out.parse_error


def test_missing_flag_falls_back():
    raw = '{"message": "no flag here"}'
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == raw
    assert out.parse_error is not None
    assert "push_message_sent" in out.parse_error


def test_non_bool_flag_falls_back():
    raw = '{"push_message_sent": "yes", "message": "x"}'
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.parse_error is not None


def test_non_string_message_falls_back():
    raw = '{"push_message_sent": false, "message": 42}'
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.parse_error is not None


def test_extra_fields_ignored():
    raw = (
        '{"push_message_sent": false, "message": "ok", '
        '"extra": 1, "diagnostics": {"a": 2}}'
    )
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == "ok"
    assert out.parse_error is None


def test_missing_message_defaults_empty_when_flag_present():
    raw = '{"push_message_sent": true}'
    out = parse_brain_output(raw)
    assert out.push_message_sent is True
    assert out.message == ""
    assert out.parse_error is None


def test_trailing_silent_legacy_falls_through_to_raw():
    """Legacy brains emitting summary + 'SILENT' last line now go via the
    parser's fallback. Operator log surfaces parse_error; the raw text is
    delivered so users aren't dropped silently. Heartbeat-runner-level
    suppression has been removed in favor of the contract."""
    raw = "Sent. Message ID 447. 3 decisions locked.\n\nSILENT"
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == raw
    assert out.parse_error is not None
