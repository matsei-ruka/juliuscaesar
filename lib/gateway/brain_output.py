"""Parser for the structured brain-output contract.

Contract: every brain emits a single JSON object on stdout:

    {"push_message_sent": <bool>, "message": <str>}

- ``push_message_sent: true`` — the brain already pushed the user-facing
  output through PushNotification (or equivalent). The framework MUST NOT
  re-deliver. The ``message`` field is treated as an audit / transcript
  record of what was pushed.
- ``push_message_sent: false`` — the framework delivers ``message`` to the
  channel as the reply.

If parsing fails (invalid JSON, missing fields, wrong types), the parser
falls back to ``push_message_sent=False`` with the raw stdout as
``message`` so users are never silently dropped, and exposes
``parse_error`` so the runtime can log a warning.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*)\n```\s*$", re.DOTALL)


@dataclass(frozen=True)
class BrainOutput:
    push_message_sent: bool
    message: str
    parse_error: str | None = None


def parse_brain_output(raw: str | None) -> BrainOutput:
    stripped = (raw or "").strip()
    if not stripped:
        return BrainOutput(push_message_sent=False, message="")

    fence_match = _FENCE_RE.match(stripped)
    if fence_match:
        stripped = fence_match.group(1).strip()

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
