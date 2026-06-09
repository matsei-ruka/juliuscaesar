"""Shared HTTP helper for channel modules."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


def http_json(
    url: str,
    *,
    token: str | None = None,
    data: dict[str, Any] | None = None,
    timeout: int = 15,
    extra_headers: dict[str, str] | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    body = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    if data is not None:
        body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # JSON APIs (Telegram, Discord, Slack) return a structured error body
        # on 4xx — e.g. Telegram 400 {"ok":false,"description":"...message is
        # not modified..."}. urlopen raises before the caller can inspect it,
        # so callers that branch on data["ok"]/data["description"] (no-op edit
        # detection, parse_mode fallback) never run and instead treat every
        # 4xx as a hard failure. Read the body and return it as JSON so that
        # API-level errors flow through normally; re-raise only when the body
        # is absent or not JSON (genuine transport/HTML errors).
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raise exc
        if not raw:
            raise exc
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raise exc
    return json.loads(raw) if raw else {}
