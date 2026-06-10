"""Narrow OpenRouter brain used by unsafe-triage fallback."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import requests

from ..config import env_value, load_config_cached
from ..queue import Event
from .base import AdapterFailure, Brain, BrainResult, now_iso


OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterBrain(Brain):
    """Stateless chat-completions brain for unsafe fallback dispatch.

    This adapter intentionally has no session support. It is meant as a narrow
    escape hatch for messages that would otherwise be silently dropped by an
    unsafe triage verdict.
    """

    name = "openrouter"

    def validate(self) -> None:
        # .env-first, secret-strict (audit G-P1 / feature 8): never resolve
        # the key from a sibling shell's exported environment.
        api_key = env_value(self.instance_dir, "OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY missing")

    def invoke(
        self,
        *,
        event: Event,
        model: str | None,
        resume_session: str | None,
        timeout_seconds: int,
        log_path: Path,
        log_event: Callable[[str], None] | None = None,
    ) -> BrainResult:
        self.validate()
        cfg = load_config_cached(self.instance_dir)
        selected_model = model or "x-ai/grok-4-fast"
        timeout = self.override.timeout_seconds or cfg.triage.unsafe_fallback_timeout_seconds
        timeout = timeout or timeout_seconds
        prompt = self.prompt_for_event(event)
        # .env-first, secret-strict (audit G-P1 / feature 8): never resolve
        # the key from a sibling shell's exported environment.
        api_key = env_value(self.instance_dir, "OPENROUTER_API_KEY")
        log = log_event or (lambda _msg: None)
        start = now_iso()
        wall_start = time.monotonic()
        with log_path.open("ab") as binlog:
            binlog.write(
                f"[{start}] adapter start event={event.id} brain={self.name} "
                f"model={selected_model}\n".encode()
            )
            binlog.flush()
        log(
            f"adapter spawn event={event.id} brain={self.name} pid={os.getpid()} "
            f"model={selected_model} resume=no"
        )
        try:
            response = requests.post(
                OPENROUTER_CHAT_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": selected_model,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            choices = payload.get("choices") or []
            text = ""
            if choices:
                message = choices[0].get("message") or {}
                text = str(message.get("content") or "")
            duration = time.monotonic() - wall_start
            log(
                f"adapter exit event={event.id} brain={self.name} pid={os.getpid()} "
                f"rc=0 duration={duration:.1f}s"
            )
            return BrainResult(response=text.strip(), session_id=None)
        except requests.Timeout as exc:
            duration = time.monotonic() - wall_start
            log(
                f"adapter timeout event={event.id} brain={self.name} "
                f"pid={os.getpid()} duration={duration:.1f}s timeout={timeout}s"
            )
            raise TimeoutError(f"adapter timeout after {timeout}s") from exc
        except Exception as exc:  # noqa: BLE001
            duration = time.monotonic() - wall_start
            msg = f"openrouter brain failed: {exc}"
            with log_path.open("ab") as binlog:
                binlog.write(f"{msg}\n".encode())
            log(
                f"adapter exit event={event.id} brain={self.name} pid={os.getpid()} "
                f"rc=1 duration={duration:.1f}s"
            )
            raise AdapterFailure(self.name, 1, msg) from exc
