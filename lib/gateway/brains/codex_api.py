"""Brain wrapper for the direct OpenAI Responses API path.

Unlike the other brains in this package, ``codex_api`` does not shell out to
a CLI. It calls the API directly through
:class:`lib.gateway.adapters.codex_api.CodexApiAdapter`, which in turn uses
the bearer token served by :class:`lib.codex_auth.client.CodexAuthClient`.

We override :meth:`Brain.invoke` because the default implementation assumes
a subprocess adapter on disk and we do not have one — there is nothing to
exec.

Failure modes:
- ``RefreshExpired``  → :class:`AdapterFailure` rc=10 (re-login required)
- ``ResponsesError 401`` → :class:`AdapterFailure` rc=11 (auth fell over)
- other ``ResponsesError`` → :class:`AdapterFailure` rc=12 (API error)
- ``CodexAuthError`` (parse / file) → :class:`AdapterFailure` rc=2

Adapter codes were chosen so the gateway's recovery classifier can distinguish
permanent (re-login) vs. transient (network / 5xx) failures and fall back.
"""

from __future__ import annotations

import os
import time
from typing import Callable

from codex_auth.errors import (
    AuthFileMissing,
    AuthModeUnsupported,
    CodexAuthError,
    RefreshExpired,
)

from ..adapters.codex_api import CodexApiAdapter, DEFAULT_MAIN_CHAT_MODEL
from ..queue import Event
from .base import AdapterFailure, Brain, BrainResult, now_iso


# Reserved exit-code conventions for the codex_api brain. Callers (recovery
# classifier, fallbacks) match on these so the meaning of each rc is stable.
RC_RELOGIN_REQUIRED = 10
RC_UNAUTHORIZED = 11
RC_API_ERROR = 12
RC_AUTH_FILE = 2


# Per docs/specs/codex-main-brain-hardening.md §Phase 5: short system
# instruction for the direct-API chat path. The detailed L1 preamble lives
# in the user-message body via `prompt_for_event`; this `instructions` slot
# stays terse so the model knows the calling contract.
CODEX_API_INSTRUCTIONS = (
    "You are the JuliusCaesar instance assistant responding via the direct "
    "Responses API. You are stateless: any 'Recent conversation history' "
    "block in the prompt is your only continuity — use it for context, "
    "do not echo it back. Answer the user; do not narrate gateway metadata."
)


class CodexApiBrain(Brain):
    name = "codex_api"
    needs_l1_preamble = True

    def __init__(self, instance_dir, *, override=None, adapter: CodexApiAdapter | None = None,
                 codex_auth_cfg=None):
        super().__init__(instance_dir, override=override)
        self._adapter = adapter
        self._codex_auth_cfg = codex_auth_cfg

    def adapter_path(self):  # type: ignore[override]
        # No on-disk adapter — bypass Brain.validate()'s file checks.
        return None

    def validate(self) -> None:  # type: ignore[override]
        return None

    def _build_adapter(self, *, model: str | None, timeout_seconds: int) -> CodexApiAdapter:
        if self._adapter is not None:
            return self._adapter
        return CodexApiAdapter(
            codex_auth_cfg=self._codex_auth_cfg,
            default_model=model or DEFAULT_MAIN_CHAT_MODEL,
            timeout_seconds=timeout_seconds,
        )

    def invoke(  # type: ignore[override]
        self,
        *,
        event: Event,
        model: str | None,
        resume_session: str | None,
        timeout_seconds: int,
        log_path,
        log_event: Callable[[str], None] | None = None,
    ) -> BrainResult:
        # resume_session is intentionally ignored: each Responses API call is
        # stateless. Conversation continuity is rebuilt by prepending the
        # transcript-priming block (Phase 5 of the codex-main-brain spec).
        prompt = self.prompt_for_event(event)
        if event.conversation_id:
            priming = self._build_transcript_priming(event)
            if priming:
                prompt = priming + "\n\n" + prompt
        log = log_event or (lambda _msg: None)
        timeout = self.override.timeout_seconds or timeout_seconds
        start = now_iso()
        wall_start = time.monotonic()

        with log_path.open("ab") as binlog:
            binlog.write(
                f"[{start}] adapter start event={event.id} brain={self.name} model={model or '-'}\n".encode()
            )
            binlog.flush()
            try:
                os.fsync(binlog.fileno())
            except OSError:
                pass
            log(
                f"adapter spawn event={event.id} brain={self.name} pid={os.getpid()} "
                f"model={model or '-'} resume=no"
            )
            try:
                adapter = self._build_adapter(model=model, timeout_seconds=timeout)
                result = adapter.run(
                    prompt, model=model, instructions=CODEX_API_INSTRUCTIONS
                )
            except RefreshExpired as exc:
                duration = time.monotonic() - wall_start
                msg = f"refresh expired: {exc} — operator must run `codex login`"
                binlog.write(f"{msg}\n".encode())
                log(
                    f"adapter exit event={event.id} brain={self.name} pid={os.getpid()} "
                    f"rc={RC_RELOGIN_REQUIRED} duration={duration:.1f}s"
                )
                raise AdapterFailure(self.name, RC_RELOGIN_REQUIRED, msg)
            except AuthFileMissing as exc:
                msg = str(exc)
                binlog.write(f"{msg}\n".encode())
                raise AdapterFailure(self.name, RC_AUTH_FILE, msg)
            except AuthModeUnsupported as exc:
                msg = str(exc)
                binlog.write(f"{msg}\n".encode())
                raise AdapterFailure(self.name, RC_AUTH_FILE, msg)
            except CodexAuthError as exc:
                msg = f"codex_auth error: {exc}"
                binlog.write(f"{msg}\n".encode())
                raise AdapterFailure(self.name, RC_API_ERROR, msg)
            except Exception as exc:  # noqa: BLE001 — surface to recovery
                # ResponsesError lives in codex_auth.responses; catch by name
                # to avoid an upward import.
                rc = RC_UNAUTHORIZED if getattr(exc, "status", 0) == 401 else RC_API_ERROR
                msg = f"responses_api error: {exc}"
                binlog.write(f"{msg}\n".encode())
                raise AdapterFailure(self.name, rc, msg)
            duration = time.monotonic() - wall_start
            log(
                f"adapter exit event={event.id} brain={self.name} pid={os.getpid()} "
                f"rc=0 duration={duration:.1f}s model={result.model}"
            )
            if result.usage:
                log(f"codex_api usage event={event.id} usage={result.usage}")
        return BrainResult(response=result.text.strip(), session_id=None)
