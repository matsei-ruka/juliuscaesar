"""Adapter-failure classifier — calls OpenRouter for triage.

Includes a cheap regex prefilter for the four most common stderr signatures so
we skip the LLM call (and ~1s of latency) on known failure modes. The LLM is
only invoked on a prefilter miss.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.error import URLError

from ..config import GatewayConfig, env_value
from ..queue import Event


_CLASSIFIER_TIMEOUT_SECONDS = 5
_STDERR_TAIL_BYTES = 8 * 1024
_STDERR_TAIL_LINES = 80
_EVENT_PREVIEW_CHARS = 800
_PROMPT_PATH = Path(__file__).resolve().parent / "prompt.md"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

ClassificationKind = Literal[
    "transient",
    "session_expired",
    "session_missing",
    "bad_input",
    "unknown",
]


@dataclass(frozen=True)
class Classification:
    kind: ClassificationKind
    confidence: float
    extracted: dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    source: str = "llm"  # "regex" | "llm" | "fallback"


_REGEX_RULES: list[tuple[ClassificationKind, re.Pattern[str], float]] = [
    (
        "session_missing",
        re.compile(
            r"(no conversation found with session id|session [^\s]+ (?:not found|unknown)|failed to resume|unknown session)",
            re.IGNORECASE,
        ),
        0.97,
    ),
    (
        "session_expired",
        re.compile(
            r"(please run.*?/login|authentication failed|401\s*unauthorized|session has expired|invalid api key)",
            re.IGNORECASE,
        ),
        0.95,
    ),
    (
        "transient",
        re.compile(
            r"(econnreset|eai_again|connection reset|context deadline exceeded|timeout|temporary failure|503\b|502\b|504\b)",
            re.IGNORECASE,
        ),
        0.9,
    ),
    (
        "bad_input",
        re.compile(
            r"(image exceeds maximum size|invalid base64|payload too large|unsupported file type|mime type mismatch|schema validation)",
            re.IGNORECASE,
        ),
        0.92,
    ),
]


_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_LOGIN_URL_RE = re.compile(r"https://(?:claude\.ai|console\.anthropic\.com|openrouter\.ai)/[^\s\"'<>]+")
_ALLOWED_LOGIN_HOSTS = ("claude.ai", "console.anthropic.com", "openrouter.ai")


def _truncate(stderr: str) -> str:
    if not stderr:
        return ""
    if len(stderr) > _STDERR_TAIL_BYTES:
        stderr = stderr[-_STDERR_TAIL_BYTES:]
    lines = stderr.splitlines()
    if len(lines) > _STDERR_TAIL_LINES:
        lines = lines[-_STDERR_TAIL_LINES:]
    return "\n".join(lines)


def _extract_for(kind: ClassificationKind, stderr: str) -> dict[str, Any]:
    extracted: dict[str, Any] = {}
    if kind == "session_missing":
        m = _UUID_RE.search(stderr)
        if m:
            extracted["session_id"] = m.group(0)
    elif kind == "session_expired":
        m = _LOGIN_URL_RE.search(stderr)
        if m:
            url = m.group(0).rstrip(".,:;")
            if any(host in url for host in _ALLOWED_LOGIN_HOSTS):
                extracted["login_url"] = url
    return extracted


def regex_prefilter(stderr: str) -> Classification | None:
    """Return a Classification if a known stderr signature matches; else None."""
    if not stderr:
        return None
    for kind, pattern, confidence in _REGEX_RULES:
        if pattern.search(stderr):
            return Classification(
                kind=kind,
                confidence=confidence,
                extracted=_extract_for(kind, stderr),
                raw=pattern.pattern,
                source="regex",
            )
    return None


def _load_prompt_template() -> str:
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def _render_prompt(event: Event, stderr_tail: str) -> str:
    template = _load_prompt_template()
    event_preview = (event.content or "")[:_EVENT_PREVIEW_CHARS]
    return template.replace("{event_content}", event_preview).replace("{stderr_tail}", stderr_tail)


def _parse_classifier_json(raw: str) -> Classification | None:
    if not raw:
        return None
    match = re.search(r"\{[\s\S]*?\}", raw)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "unknown")
    if kind not in ("transient", "session_expired", "session_missing", "bad_input", "unknown"):
        kind = "unknown"
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    extracted = data.get("extracted") if isinstance(data.get("extracted"), dict) else {}
    return Classification(
        kind=kind,  # type: ignore[arg-type]
        confidence=max(0.0, min(1.0, confidence)),
        extracted=extracted,
        raw=raw[:400],
        source="llm",
    )


def llm_classify(
    event: Event,
    stderr_tail: str,
    *,
    config: GatewayConfig,
    instance_dir: Path,
) -> Classification | None:
    """Call OpenRouter to classify. Returns None on outage / parse failure."""
    api_key = env_value(instance_dir, config.triage.openrouter_api_key_env)
    if not api_key:
        return None
    prompt = _render_prompt(event, stderr_tail)
    body = {
        "model": config.triage.openrouter_model,
        "messages": [
            {"role": "system", "content": "Output exactly one JSON object on one line."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
    }
    try:
        req = urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/matsei-ruka/juliuscaesar",
                "X-Title": "JuliusCaesar Gateway Recovery",
            },
        )
        with urllib.request.urlopen(req, timeout=_CLASSIFIER_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (URLError, TimeoutError, ConnectionError, OSError):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    choices = payload.get("choices") or []
    if not choices:
        return None
    text = str((choices[0].get("message") or {}).get("content") or "")
    return _parse_classifier_json(text)


def classify(
    event: Event,
    stderr_tail: str,
    *,
    config: GatewayConfig,
    instance_dir: Path,
    confidence_floor: float = 0.6,
) -> Classification:
    """Run the regex prefilter, then the LLM. Falls back to `unknown`."""
    tail = _truncate(stderr_tail)
    prefilter = regex_prefilter(tail)
    if prefilter is not None:
        return prefilter
    llm = llm_classify(event, tail, config=config, instance_dir=instance_dir)
    if llm is None:
        # Classifier outage — caller decides whether to fall back. Returning
        # `unknown` with a sentinel source lets the dispatcher keep its old
        # blind-retry contract on outage.
        return Classification(
            kind="unknown",
            confidence=0.0,
            extracted={},
            raw="",
            source="fallback",
        )
    if llm.confidence < confidence_floor:
        return Classification(
            kind="unknown",
            confidence=llm.confidence,
            extracted=llm.extracted,
            raw=llm.raw,
            source="llm",
        )
    return llm
