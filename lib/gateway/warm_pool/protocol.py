"""Wire protocol for `claude -p --input-format=stream-json --output-format=stream-json`.

Empirically determined (claude-code 2.1.119): newline-delimited JSON in both
directions. One user message line per turn; output stream begins with a
`system/init` event, optional `assistant` events (one per content block),
optional `rate_limit_event`, then a terminating `result` event. The same
`session_id` is reused across turns within a single process.

Spec deviation: the spec described a custom `{"type":"invoke",...}` envelope.
Real protocol uses claude's existing stream-json schema, which we adopt verbatim
to avoid forking the CLI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class InvokeResult:
    text: str
    session_id: str | None
    stop_reason: str | None
    is_error: bool
    error_text: str | None
    raw_events: tuple[dict, ...]


def encode_user_message(content: str) -> str:
    """Build the single-line JSON payload claude expects on stdin."""
    payload = {
        "type": "user",
        "message": {"role": "user", "content": content},
    }
    return json.dumps(payload, ensure_ascii=False)


def parse_event_line(line: str) -> dict | None:
    """Parse one stdout line into an event dict; return None on garbage.

    Claude occasionally interleaves non-JSON warnings on stderr — those don't
    reach us here, but defensive parsing keeps the read loop alive on the rare
    blank line or partial flush.
    """
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def is_terminal_event(event: dict) -> bool:
    """A `result` event marks the end of a turn."""
    return event.get("type") == "result"


def extract_result(events: list[dict]) -> InvokeResult:
    """Reduce a turn's worth of events into one `InvokeResult`.

    The terminal `result` event carries the final text + session_id; we fall
    back to scanning `assistant` events only if the result event is malformed
    (defensive: the assistant text is the user-facing answer either way).
    """
    result_evt = next((e for e in reversed(events) if is_terminal_event(e)), None)
    session_id = None
    for evt in events:
        sid = evt.get("session_id")
        if isinstance(sid, str):
            session_id = sid

    if result_evt is None:
        text = _join_assistant_text(events)
        return InvokeResult(
            text=text,
            session_id=session_id,
            stop_reason=None,
            is_error=True,
            error_text="no result event in stream",
            raw_events=tuple(events),
        )

    is_error = bool(result_evt.get("is_error"))
    text = result_evt.get("result")
    if not isinstance(text, str) or not text:
        text = _join_assistant_text(events)
    stop_reason = result_evt.get("stop_reason")
    error_text = None
    if is_error:
        error_text = result_evt.get("api_error_status") or result_evt.get("result") or "unknown"
    return InvokeResult(
        text=text or "",
        session_id=session_id or result_evt.get("session_id"),
        stop_reason=stop_reason if isinstance(stop_reason, str) else None,
        is_error=is_error,
        error_text=error_text if isinstance(error_text, str) else None,
        raw_events=tuple(events),
    )


def _join_assistant_text(events: list[dict]) -> str:
    chunks: list[str] = []
    for evt in events:
        if evt.get("type") != "assistant":
            continue
        message = evt.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "".join(chunks)
