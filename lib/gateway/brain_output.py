"""Parser for the structured brain-output contract.

Contract: every brain emits a single JSON object on stdout:

    {"push_message_sent": <bool>, "message": <str>}

- ``push_message_sent: true`` — the brain already pushed the user-facing
  output through PushNotification (or equivalent). The framework MUST NOT
  re-deliver. The ``message`` field is treated as an audit / transcript
  record of what was pushed.
- ``push_message_sent: false`` — the framework delivers ``message`` to the
  channel as the reply.

If parsing fails (invalid JSON, missing fields, wrong types), the parser falls
back to ``push_message_sent=False`` with the raw stdout as ``message`` so users
are never silently dropped, and exposes ``parse_error`` so the runtime can log a
warning.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*)\n```\s*$", re.DOTALL)
_JSON_DECODER = json.JSONDecoder()
_INTERNAL_SOURCES = {"cron", "jc-events"}
_SILENT_TOKENS = {
    "SILENT",
    "SILENCE",
    "[SILENT]",
    "[NO-REPLY]",
    "[NO_REPLY]",
    "[SKIP]",
    "NO_REPLY",
    "NO-REPLY",
}


@dataclass(frozen=True)
class BrainOutput:
    push_message_sent: bool
    message: str
    parse_error: str | None = None


def parse_brain_output(raw: str | None, *, event_source: str | None = None) -> BrainOutput:
    stripped = (raw or "").strip()
    if not stripped:
        return BrainOutput(push_message_sent=False, message="")

    if _is_silent_token(stripped):
        return BrainOutput(push_message_sent=False, message="")
    if _has_internal_trailing_sentinel(stripped, event_source):
        return BrainOutput(push_message_sent=False, message="")

    fence_match = _FENCE_RE.match(stripped)
    if fence_match:
        stripped = fence_match.group(1).strip()

    embedded = _parse_embedded_contract(stripped)
    if embedded is not None:
        return embedded

    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return BrainOutput(
            push_message_sent=False,
            message=raw or "",
            parse_error=f"JSONDecodeError: {exc.msg} at line {exc.lineno} col {exc.colno}",
        )

    if not isinstance(obj, dict):
        return BrainOutput(
            push_message_sent=False,
            message=raw or "",
            parse_error=f"expected JSON object, got {type(obj).__name__}",
        )

    flag = obj.get("push_message_sent")
    if not isinstance(flag, bool):
        return BrainOutput(
            push_message_sent=False,
            message=raw or "",
            parse_error=(
                "missing or non-bool 'push_message_sent' "
                f"(got {type(flag).__name__})"
            ),
        )

    msg = obj.get("message", "")
    if not isinstance(msg, str):
        return BrainOutput(
            push_message_sent=False,
            message=raw or "",
            parse_error=f"'message' must be string, got {type(msg).__name__}",
        )

    return BrainOutput(push_message_sent=flag, message=msg)


def _parse_embedded_contract(text: str) -> BrainOutput | None:
    """Recover when a brain emits prose plus the JSON envelope.

    Models commonly produce a visible answer and then append the gateway JSON
    object. Relaying the raw stdout leaks the contract to Telegram and can make
    the user see an apparent duplicate. We therefore scan for the last valid
    contract object and let that object decide delivery.
    """

    last: tuple[int, int, dict[str, Any]] | None = None
    for match in re.finditer(r"\{", text):
        start = match.start()
        try:
            obj, end = _JSON_DECODER.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if _looks_like_contract(obj):
            last = (start, start + end, obj)
    if last is None:
        return None

    start, end, obj = last
    msg = obj.get("message", "")
    prefix = text[:start].strip()
    suffix = text[end:].strip()
    parse_error = None
    if prefix or suffix:
        parse_error = "recovered JSON envelope with surrounding stdout"
    return BrainOutput(
        push_message_sent=bool(obj["push_message_sent"]),
        message=msg,
        parse_error=parse_error,
    )


def _looks_like_contract(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if not isinstance(obj.get("push_message_sent"), bool):
        return False
    msg = obj.get("message", "")
    return isinstance(msg, str)


def _is_silent_token(text: str) -> bool:
    return text.strip().upper() in _SILENT_TOKENS


def _has_internal_trailing_sentinel(text: str, event_source: str | None) -> bool:
    if str(event_source or "") not in _INTERNAL_SOURCES:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines and _is_silent_token(lines[-1]))


def push_marker_sent(path: str | Path | None) -> bool:
    if not path:
        return False
    try:
        marker = Path(path)
        return marker.is_file() and marker.stat().st_size > 0
    except OSError:
        return False
