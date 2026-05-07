"""Generic HTTP triage backend with pluggable provider protocols."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

from ..config import TriageConfig, env_value
from .base import TriageBackend, TriageResult, failure_result, parse_triage_json, render_prompt
from .protocols import get_protocol


class ApiClassifierTriage(TriageBackend):
    name = "api_classifier"

    def __init__(
        self,
        cfg: TriageConfig,
        instance_dir: Path,
        *,
        protocol_name: str | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
        max_tokens: int | None = None,
        extra_headers: dict[str, str] | None = None,
        name: str | None = None,
    ):
        self.cfg = cfg
        self.instance_dir = instance_dir
        self.protocol_name = protocol_name or cfg.protocol
        self.protocol = get_protocol(self.protocol_name)
        self.base_url = base_url or cfg.base_url
        self.api_key_env = api_key_env or cfg.api_key_env
        self.model = model or cfg.model
        self.timeout_seconds = int(timeout_seconds if timeout_seconds is not None else cfg.timeout_seconds)
        self.max_tokens = max_tokens if max_tokens is not None else cfg.max_tokens
        self.extra_headers = dict(extra_headers or {})
        if name is not None:
            self.name = name

    def _api_key(self) -> str:
        return env_value(self.instance_dir, self.api_key_env)

    def classify(self, message: str) -> TriageResult:
        api_key = self._api_key()
        if not api_key:
            return _failure(f"missing {self.api_key_env}")
        prompt = render_prompt(message)
        try:
            body = self.protocol.encode(
                prompt,
                model=self.model,
                max_tokens=self.max_tokens,
            )
        except ValueError as exc:
            return _failure(f"{self.protocol_name} config error: {exc}")
        headers = self.protocol.headers(api_key)
        headers.update(self.extra_headers)
        try:
            req = urllib.request.Request(
                self.protocol.url(self.base_url),
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            return _failure(f"{self.name} HTTP {exc.code}: {exc.reason}")
        except (URLError, TimeoutError, ConnectionError) as exc:
            return _failure(f"{self.name} unreachable: {exc}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return _failure(f"{self.name} bad response: {exc}", raw=raw)
        try:
            text = self.protocol.decode(payload)
        except (KeyError, TypeError, ValueError) as exc:
            return _failure(f"{self.name} bad payload: {exc}", raw=raw)
        result = parse_triage_json(text)
        if result is not None:
            return result
        if self.protocol_name == "anthropic" and payload.get("stop_reason") == "max_tokens":
            return _failure("anthropic truncated triage output", raw=text)
        return _failure(f"{self.name} unparseable triage output", raw=text)


def _failure(reason: str, *, raw: str | None = None) -> TriageResult:
    return failure_result(reason, raw=raw)
