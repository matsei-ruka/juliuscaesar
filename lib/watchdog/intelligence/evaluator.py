"""Triage-backed evaluator for watchdog health decisions."""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError, URLError

from gateway.config import GatewayConfig, env_value
from gateway.triage.openrouter import OPENROUTER_BASE_URL
from gateway.triage.protocols import get_protocol

from .config import IntelligenceConfig
from .models import Decision, EventSummary, Snapshot


PROMPT_PATH = Path(__file__).resolve().parent / "prompt.md"
AUTH_RE = re.compile(
    r"(authentication|auth failed|please run.*login|/login|session (?:has )?expired|401|unauthorized|invalid api key|refresh token)",
    re.IGNORECASE,
)
TIMEOUT_RE = re.compile(r"(adapter timeout|timeout|timed out|deadline exceeded)", re.IGNORECASE)
FAIL_RE = re.compile(r"(dispatch failed|recovery fail|no models loaded|crash|traceback|non-zero|rc=)", re.IGNORECASE)


class Evaluator:
    def __init__(
        self,
        instance_dir: Path,
        *,
        gateway_config: GatewayConfig,
        intelligence_config: IntelligenceConfig,
    ):
        self.instance_dir = instance_dir
        self.gateway_config = gateway_config
        self.intelligence_config = intelligence_config

    def evaluate_event(self, snapshot: Snapshot, summary: EventSummary) -> Decision:
        heuristic = self._heuristic(snapshot, summary)
        if not self.intelligence_config.use_triage_model:
            return heuristic
        llm = self._triage_model_decision(snapshot, summary)
        if llm is None:
            return heuristic
        # Auth and hard brain failures from deterministic evidence should not
        # be softened by a weak/ambiguous model output.
        if heuristic.kind in ("auth_expired", "brain_unhealthy") and llm.confidence < 0.8:
            return heuristic
        return llm

    def _heuristic(self, snapshot: Snapshot, summary: EventSummary) -> Decision:
        text = "\n".join(
            [
                summary.error,
                *[
                    entry.msg or entry.raw
                    for entry in snapshot.logs
                    if entry.event_id in (None, summary.event.id)
                ],
            ]
        )
        if AUTH_RE.search(text):
            return Decision(
                kind="auth_expired",
                confidence=0.96,
                severity="critical",
                user_visible=True,
                should_switch_brain=True,
                summary="brain authentication appears expired",
                source="heuristic",
            )
        if FAIL_RE.search(text) and summary.status in ("failed", "queued"):
            return Decision(
                kind="brain_unhealthy",
                confidence=0.86,
                severity="warning",
                user_visible=True,
                should_switch_brain=True,
                summary="brain failed before completing the request",
                source="heuristic",
            )
        if TIMEOUT_RE.search(text) and summary.status in ("failed", "queued"):
            return Decision(
                kind="brain_unhealthy",
                confidence=0.82,
                severity="warning",
                user_visible=True,
                should_switch_brain=True,
                summary="brain adapter timed out",
                source="heuristic",
            )
        if summary.status == "running":
            return Decision(
                kind="long_running",
                confidence=0.78,
                severity="info",
                user_visible=True,
                should_switch_brain=False,
                summary="request is still running past the notice threshold",
                source="heuristic",
            )
        return Decision(
            kind="unknown",
            confidence=0.35,
            severity="info",
            user_visible=False,
            should_switch_brain=False,
            summary="insufficient watchdog evidence",
            source="heuristic",
        )

    def _triage_model_decision(self, snapshot: Snapshot, summary: EventSummary) -> Decision | None:
        cfg = self.gateway_config.triage
        if cfg.backend in ("none", "", "always"):
            return None
        if cfg.backend == "openrouter":
            protocol_name = "openai_compat"
            base_url = OPENROUTER_BASE_URL
            api_key_env = cfg.openrouter_api_key_env
            model = cfg.openrouter_model
            timeout = cfg.openrouter_timeout_seconds
            max_tokens = 200
            headers = {
                "HTTP-Referer": "https://github.com/matsei-ruka/juliuscaesar",
                "X-Title": "JuliusCaesar Watchdog",
            }
        elif cfg.backend == "api_classifier":
            protocol_name = cfg.protocol
            base_url = cfg.base_url
            api_key_env = cfg.api_key_env
            model = cfg.model
            timeout = cfg.timeout_seconds
            max_tokens = cfg.max_tokens or 200
            headers = {}
        elif cfg.backend == "ollama":
            return self._ollama_decision(snapshot, summary)
        elif cfg.backend in ("codex_api", "codex-api"):
            return self._codex_api_decision(snapshot, summary)
        elif cfg.backend == "claude-channel":
            return self._claude_channel_decision(snapshot, summary)
        else:
            return None
        api_key = env_value(self.instance_dir, api_key_env)
        if not api_key or not base_url or not model:
            return None
        protocol = get_protocol(protocol_name)
        prompt = _render_prompt(snapshot, summary)
        try:
            body = protocol.encode(prompt, model=model, max_tokens=max_tokens)
        except ValueError:
            return None
        req_headers = protocol.headers(api_key)
        req_headers.update(headers)
        try:
            req = urllib.request.Request(
                protocol.url(base_url),
                data=json.dumps(body).encode("utf-8"),
                headers=req_headers,
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError, ConnectionError, OSError):
            return None
        try:
            payload = json.loads(raw)
            text = protocol.decode(payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None
        parsed = parse_decision_json(text)
        if parsed is None:
            return None
        return replace(parsed, source="triage_model")

    def _ollama_decision(self, snapshot: Snapshot, summary: EventSummary) -> Decision | None:
        cfg = self.gateway_config.triage
        prompt = _render_prompt(snapshot, summary)
        body = {
            "model": cfg.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0},
        }
        try:
            req = urllib.request.Request(
                cfg.ollama_host.rstrip("/") + "/api/generate",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=cfg.ollama_timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw)
        except (OSError, TimeoutError, ConnectionError, json.JSONDecodeError):
            return None
        parsed = parse_decision_json(str(payload.get("response") or ""))
        return replace(parsed, source="triage_model") if parsed is not None else None

    def _codex_api_decision(self, snapshot: Snapshot, summary: EventSummary) -> Decision | None:
        try:
            from codex_auth import CodexAuthClient, ResponsesClient
            from gateway.triage.codex_api import _model_from_cfg
        except Exception:
            return None
        cfg = self.gateway_config.triage
        try:
            client = ResponsesClient(
                CodexAuthClient(
                    auth_file=self.gateway_config.codex_auth.auth_file,
                    client_id_override=self.gateway_config.codex_auth.client_id_override,
                    refresh_skew_seconds=self.gateway_config.codex_auth.refresh_skew_seconds,
                ),
                default_model=_model_from_cfg(cfg),
                timeout_seconds=max(5, int(cfg.openrouter_timeout_seconds or 10)),
            )
            result = client.complete(
                _render_prompt(snapshot, summary),
                model=_model_from_cfg(cfg),
                instructions="Output exactly one JSON object on one line.",
            )
        except Exception:
            return None
        parsed = parse_decision_json(result.text or "")
        return replace(parsed, source="triage_model") if parsed is not None else None

    def _claude_channel_decision(self, snapshot: Snapshot, summary: EventSummary) -> Decision | None:
        cfg = self.gateway_config.triage
        body = {"message": _render_prompt(snapshot, summary)}
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{cfg.claude_triage_port}/classify",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (OSError, TimeoutError, ConnectionError):
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"text": raw}
        text = payload.get("text") if isinstance(payload, dict) else str(payload)
        parsed = parse_decision_json(str(text or "")) or parse_decision_json(raw)
        return replace(parsed, source="triage_model") if parsed is not None else None


def parse_decision_json(raw: str) -> Decision | None:
    match = re.search(r"\{[\s\S]*?\}", raw or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "unknown")
    if kind not in {
        "healthy",
        "brain_unhealthy",
        "auth_expired",
        "long_running",
        "transient_slow",
        "unknown",
    }:
        kind = "unknown"
    severity = str(data.get("severity") or "info")
    if severity not in {"info", "warning", "critical"}:
        severity = "info"
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return Decision(
        kind=kind,  # type: ignore[arg-type]
        confidence=max(0.0, min(1.0, confidence)),
        severity=severity,  # type: ignore[arg-type]
        user_visible=bool(data.get("user_visible", False)),
        should_switch_brain=bool(data.get("should_switch_brain", False)),
        summary=str(data.get("summary") or "")[:200],
        source="triage_model",
    )


def _render_prompt(snapshot: Snapshot, summary: EventSummary) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8") if PROMPT_PATH.exists() else "{snapshot}"
    payload = {
        "event": {
            "id": summary.event.id,
            "source": summary.event.source,
            "status": summary.status,
            "age_seconds": int(summary.age_seconds),
            "brain": summary.brain_spec,
            "error": summary.error[:500],
            "content_preview": (summary.event.content or "")[:500],
            "meta_keys": sorted(summary.meta.keys()),
        },
        "logs": [
            {
                "ts": entry.ts,
                "kind": entry.kind,
                "event_id": entry.event_id,
                "brain": entry.brain,
                "msg": (entry.msg or entry.raw)[:500],
            }
            for entry in snapshot.logs[-20:]
        ],
    }
    return template.replace("{snapshot}", json.dumps(payload, sort_keys=True))
