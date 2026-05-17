"""Shared HTTP helper for channel modules."""

from __future__ import annotations

import json
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw else {}
