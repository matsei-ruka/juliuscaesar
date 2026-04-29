"""Thin wrapper around the ChatGPT-subscription Responses endpoint.

Spec scope said ``api.openai.com/v1/responses``. In practice, the bearer
token issued for ``auth_mode=chatgpt`` is **not** authorized for the public
``api.openai.com`` Responses API ("Missing scopes: api.responses.write").
The Codex CLI itself talks to ``https://chatgpt.com/backend-api/codex`` —
the same endpoint that backs the subscription. We follow suit.

Differences vs. the public Responses API that this module hides from callers:

- ``input`` is a ``list[{role, content}]``, not a string.
- ``instructions`` is required.
- Requests must set ``store: false`` and ``stream: true`` — synchronous calls
  return ``400``. We do the streaming SSE accumulation internally so callers
  still get a single ``ResponseResult`` back, matching the simpler API
  shape from the spec.
- ``chatgpt-account-id`` header is required.
- Model catalog is the Codex one (``gpt-5.4-mini`` etc.), not generic
  OpenAI catalog. The launch model is ``gpt-5.4-mini`` (cheapest).

Streaming is opaque to the caller. We accumulate ``response.output_text.delta``
events into a buffer, then return when ``response.completed`` arrives.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

from .client import CodexAuthClient
from .errors import RefreshExpired


# ChatGPT-subscription Responses endpoint — see module docstring.
RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
# Cheapest Codex catalog model with sane reasoning. ``gpt-5.4-mini`` is the
# smallest of the gpt-5.4 family per ``~/.codex/models_cache.json``.
DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_TIMEOUT_SECONDS = 60


class ResponsesError(Exception):
    """Raised on any non-2xx response from the Codex Responses endpoint."""

    def __init__(self, status: int, message: str, body: str = ""):
        self.status = status
        self.body = body
        super().__init__(f"codex responses {status}: {message}")


@dataclass(frozen=True)
class ResponseResult:
    text: str
    raw: dict[str, Any]
    model: str
    usage: dict[str, Any] | None


class ResponsesClient:
    """Synchronous facade over the streaming SSE endpoint.

    Reuses :class:`CodexAuthClient` for bearer-token retrieval. On a 401, we
    force a refresh and retry once; persistent 401 surfaces as
    ``ResponsesError`` so the gateway can fall back to the legacy adapter.
    """

    def __init__(
        self,
        auth: CodexAuthClient,
        *,
        default_model: str = DEFAULT_MODEL,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        url: str = RESPONSES_URL,
    ):
        self.auth = auth
        self.default_model = default_model
        self.timeout_seconds = timeout_seconds
        self.url = url

    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ResponseResult:
        body: dict[str, Any] = {
            "model": model or self.default_model,
            # ChatGPT-subscription Responses requires a list of role/content
            # parts. Single-string ``input`` is rejected with 400.
            "input": [{"role": "user", "content": prompt}],
            # Required by the endpoint — empty string is fine but the field
            # must exist; pass through caller-provided instructions when set.
            "instructions": instructions or "Reply with the user's request directly.",
            "store": False,
            "stream": True,
        }
        if extra:
            body.update(extra)
        return self._request(body)

    # --- internals --------------------------------------------------------

    def _request(self, body: dict[str, Any]) -> ResponseResult:
        token = self.auth.get_bearer()
        account_id = self._account_id()
        status, raw = self._post(token, account_id, body)
        if status == 401:
            try:
                refreshed_state = self.auth.force_refresh()
            except RefreshExpired:
                raise ResponsesError(
                    401,
                    "unauthorized; re-login required",
                    raw.decode("utf-8", errors="replace"),
                )
            account_id = refreshed_state.chatgpt_account_id or account_id
            status, raw = self._post(refreshed_state.access_token, account_id, body)
        if 200 <= status < 300:
            return _accumulate_stream(raw, body.get("model", self.default_model))
        message = _extract_error_message(raw)
        raise ResponsesError(
            status,
            message or "request failed",
            raw.decode("utf-8", errors="replace"),
        )

    def _account_id(self) -> str:
        state = self.auth.read_state()
        return state.chatgpt_account_id or ""

    def _post(self, token: str, account_id: str, body: dict[str, Any]) -> tuple[int, bytes]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {token}",
            "OpenAI-Beta": "responses=v1",
        }
        if account_id:
            headers["chatgpt-account-id"] = account_id
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode("utf-8"), headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            data = b""
            try:
                data = exc.read()
            except Exception:  # noqa: BLE001
                pass
            return exc.code, data
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise ResponsesError(0, f"network error: {exc}") from exc


# --- SSE accumulation --------------------------------------------------------

def _accumulate_stream(raw: bytes, requested_model: str) -> ResponseResult:
    """Walk the SSE stream and assemble the final assistant text + metadata."""
    text_parts: list[str] = []
    completed_payload: dict[str, Any] | None = None
    last_payload: dict[str, Any] | None = None
    for event_type, payload in _iter_sse_events(raw):
        if not isinstance(payload, dict):
            continue
        last_payload = payload
        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)
        elif event_type == "response.output_text.done":
            # If we missed deltas (e.g. server only sent .done), use the
            # final ``text`` field as a fallback so we never return empty.
            if not text_parts:
                done_text = payload.get("text")
                if isinstance(done_text, str):
                    text_parts.append(done_text)
        elif event_type == "response.completed":
            completed_payload = payload.get("response") if isinstance(payload, dict) else None
        elif event_type == "response.failed" or event_type == "response.error":
            err = payload.get("error") or {}
            message = ""
            if isinstance(err, dict):
                message = str(err.get("message") or err.get("type") or "")
            raise ResponsesError(
                500, f"upstream stream error: {message or 'unknown'}",
                json.dumps(payload),
            )
    if completed_payload is None:
        # No completion event — most likely a malformed stream. Treat the
        # last seen payload as a partial response and surface the missing
        # completion as a 502 so the gateway falls back gracefully.
        raise ResponsesError(
            502,
            "stream ended without response.completed",
            json.dumps(last_payload or {}),
        )
    text = "".join(text_parts) or _extract_text(completed_payload)
    return ResponseResult(
        text=text,
        raw=completed_payload,
        model=str(completed_payload.get("model") or requested_model),
        usage=completed_payload.get("usage")
        if isinstance(completed_payload.get("usage"), dict)
        else None,
    )


def _iter_sse_events(raw: bytes) -> Iterable[tuple[str, Any]]:
    """Yield ``(event_type, parsed_data)`` per SSE block.

    SSE block separator is a blank line. Within a block, ``event:`` and
    ``data:`` lines are accumulated; the ``data:`` line is JSON-decoded.
    Multi-line ``data:`` blocks are joined with newline per the SSE spec.
    """
    text = raw.decode("utf-8", errors="replace")
    blocks = text.split("\n\n")
    for block in blocks:
        if not block.strip():
            continue
        event_type = "message"
        data_lines: list[str] = []
        for line in block.split("\n"):
            line = line.rstrip("\r")
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
        if not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            payload = None
        yield event_type, payload


def _extract_text(payload: dict[str, Any]) -> str:
    """Fallback text extractor for the final ``response`` object."""
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    pieces: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                content = item.get("content")
                if isinstance(content, list):
                    for seg in content:
                        if isinstance(seg, dict) and seg.get("type") in ("output_text", "text"):
                            text = seg.get("text")
                            if isinstance(text, str):
                                pieces.append(text)
            elif item.get("type") in ("output_text", "text"):
                text = item.get("text")
                if isinstance(text, str):
                    pieces.append(text)
    return "".join(pieces)


def _extract_error_message(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")[:500]
    if not isinstance(payload, dict):
        return ""
    # Codex backend uses ``detail`` for errors; OpenAI public API uses
    # ``error.message``. Honor both.
    detail = payload.get("detail")
    if isinstance(detail, str):
        return detail
    err = payload.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("type")
        if isinstance(msg, str):
            return msg
    return ""
