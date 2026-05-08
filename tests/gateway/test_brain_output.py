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


def test_embedded_json_relay_discards_surrounding_stdout():
    raw = (
        "Drafting the action plan reply.\n\n"
        '{"push_message_sent": false, "message": "Clean user-facing answer."}'
    )
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == "Clean user-facing answer."
    assert out.parse_error == "recovered JSON envelope with surrounding stdout"


def test_embedded_json_push_handled_discards_surrounding_stdout():
    raw = (
        "Inviato al gruppo (message_id 955).\n\n"
        '{"push_message_sent": true, "message": "Pushed Telegram message 955."}'
    )
    out = parse_brain_output(raw)
    assert out.push_message_sent is True
    assert out.message == "Pushed Telegram message 955."
    assert out.parse_error == "recovered JSON envelope with surrounding stdout"


def test_embedded_fenced_json_recovered():
    raw = (
        "Here is the final envelope:\n"
        "```json\n"
        '{"push_message_sent": false, "message": "ok"}\n'
        "```"
    )
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == "ok"
    assert out.parse_error == "recovered JSON envelope with surrounding stdout"


def test_last_embedded_json_wins_when_model_prints_duplicate_envelopes():
    raw = (
        "Drafting reply.\n\n"
        "```json\n"
        '{"push_message_sent": false, "message": "draft"}\n'
        "```\n\n"
        '{"push_message_sent": false, "message": "final"}'
    )
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == "final"
    assert out.parse_error == "recovered JSON envelope with surrounding stdout"


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


def test_envelope_silent_message_is_suppressed():
    raw = '{"push_message_sent": false, "message": "SILENT"}'
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == ""
    assert out.parse_error is None


def test_envelope_bracketed_silent_message_is_suppressed():
    raw = '{"push_message_sent": false, "message": "[SILENT]"}'
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == ""


def test_envelope_no_reply_message_is_suppressed():
    raw = '{"push_message_sent": false, "message": "[NO-REPLY]"}'
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == ""


def test_envelope_silent_with_whitespace_is_suppressed():
    raw = '{"push_message_sent": false, "message": "  SILENT  "}'
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == ""


def test_envelope_silent_lowercase_is_suppressed():
    raw = '{"push_message_sent": false, "message": "silent"}'
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == ""


def test_push_true_with_silent_audit_message_preserves_message():
    raw = '{"push_message_sent": true, "message": "SILENT"}'
    out = parse_brain_output(raw)
    assert out.push_message_sent is True
    assert out.message == "SILENT"
    assert out.parse_error is None


def test_envelope_silent_message_with_surrounding_prose_is_suppressed():
    raw = (
        "Drafting calendar brief.\n\n"
        '{"push_message_sent": false, "message": "SILENT"}'
    )
    out = parse_brain_output(raw, event_source="cron")
    assert out.push_message_sent is False
    assert out.message == ""
    assert out.parse_error == "recovered JSON envelope with surrounding stdout"


def test_envelope_non_silent_message_unchanged():
    raw = '{"push_message_sent": false, "message": "Real content."}'
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == "Real content."


def test_trailing_silent_without_internal_source_falls_back_to_raw():
    raw = "Sent. Message ID 447. 3 decisions locked.\n\nSILENT"
    out = parse_brain_output(raw)
    assert out.push_message_sent is False
    assert out.message == raw
    assert out.parse_error is not None


def test_internal_trailing_silent_suppresses_delivery():
    raw = "Sent. Message ID 447. 3 decisions locked.\n\nSILENT"
    out = parse_brain_output(raw, event_source="cron")
    assert out.push_message_sent is False
    assert out.message == ""
    assert out.parse_error is None


def test_internal_trailing_silence_alias_suppresses_delivery():
    raw = "Nothing useful to report.\n\nSILENCE"
    out = parse_brain_output(raw, event_source="jc-events")
    assert out.push_message_sent is False
    assert out.message == ""
    assert out.parse_error is None


def test_exact_silent_alias_suppresses_any_source():
    out = parse_brain_output("[no-reply]", event_source="telegram")
    assert out.push_message_sent is False
    assert out.message == ""
    assert out.parse_error is None
