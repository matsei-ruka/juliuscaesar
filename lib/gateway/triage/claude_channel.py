"""Claude-channel triage backend.

Posts to a long-running `jc-triage` MCP plugin (see
`external_plugins/jc-triage/`). The plugin runs a Claude Haiku session in a
screen and exposes an HTTP `/classify` endpoint that returns the
single-line JSON we expect.
"""

from __future__ import annotations

import json
import urllib.request
from urllib.error import URLError

from ..config import TriageConfig
from .base import TriageBackend, TriageResult, parse_triage_json


class ClaudeChannelTriage(TriageBackend):
    name = "claude-channel"

    def __init__(self, cfg: TriageConfig):
        self.cfg = cfg

    def classify(self, message: str) -> TriageResult:
        url = f"http://127.0.0.1:{self.cfg.claude_triage_port}/classify"
        body = {"message": message}
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (URLError, TimeoutError, ConnectionError) as exc:
            return _failure(f"jc-triage unreachable: {exc}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"text": raw}
        text = payload.get("text") if isinstance(payload, dict) else str(payload)
        result = parse_triage_json(str(text or "")) or parse_triage_json(raw)
        return result or _failure("jc-triage unparseable output")


def _failure(reason: str) -> TriageResult:
    return TriageResult(
        class_="quick",
        brain="claude:sonnet-4-6",
        confidence=0.0,
        reasoning=reason,
    )
