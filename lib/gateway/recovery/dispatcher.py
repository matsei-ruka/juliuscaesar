"""Recovery dispatcher — entry point called from `GatewayRuntime.dispatch_once`.

Owns the handler registry and the operator-token redemption flow. Exposed
methods:

  - `handle(event, failure)`         — classify + route to handler.
  - `maybe_consume_auth_token(event)`— pre-triage hook: returns True if the
    inbound message was an operator pasting a token in response to an active
    auth_pending row.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from . import classifier, state as state_module
from .handlers.bad_input import BadInputHandler
from .handlers.base import Defer, Fail, RecoveryContext, RecoveryDecision, Retry
from .handlers.session_expired import SessionExpiredHandler
from .handlers.session_missing import SessionMissingHandler
from .handlers.transient import TransientHandler
from .handlers.unknown import UnknownHandler

if TYPE_CHECKING:
    from ..brains.base import AdapterFailure
    from ..queue import Event


_REDEEM_TIMEOUT_SECONDS = 30


class RecoveryDispatcher:
    def __init__(self, runtime):
        self.runtime = runtime
        self.handlers: dict[str, object] = {
            "transient": TransientHandler(),
            "session_expired": SessionExpiredHandler(),
            "session_missing": SessionMissingHandler(),
            "bad_input": BadInputHandler(),
            "unknown": UnknownHandler(),
        }

    # --- main entry point -------------------------------------------------

    def handle(self, event: "Event", failure: "AdapterFailure") -> RecoveryDecision:
        ctx = self._make_context()
        classification = classifier.classify(
            event,
            failure.stderr_tail,
            config=self.runtime.config,
            instance_dir=self.runtime.instance_dir,
        )
        self.runtime.log(
            f"recovery classify id={event.id} kind={classification.kind} "
            f"conf={classification.confidence:.2f} source={classification.source}",
            event_id=event.id,
            kind="recovery_classify",
        )
        if classification.source == "fallback":
            # Classifier outage — fall back to legacy retry contract.
            return TransientHandler().handle(event, classification, ctx)
        handler = self.handlers.get(classification.kind, self.handlers["unknown"])
        try:
            return handler.handle(event, classification, ctx)
        except Exception as exc:  # noqa: BLE001
            self.runtime.log(
                f"recovery handler error id={event.id} kind={classification.kind} "
                f"reason={exc!r}",
                event_id=event.id,
                kind="recovery_handler_error",
            )
            return Retry(reason=f"handler error: {exc}", delay_seconds=10.0)

    # --- pre-triage auth-token hook --------------------------------------

    def maybe_consume_auth_token(self, event: "Event") -> bool:
        """Pre-triage hook: returns True iff `event` was an operator token."""
        operator_chat = event.user_id or self._chat_id_from_meta(event)
        if not operator_chat:
            return False
        if not state_module.looks_like_token(event.content or ""):
            return False
        from .. import queue

        conn = queue.connect(self.runtime.instance_dir)
        try:
            pending = state_module.get_active_pending(
                conn, operator_chat=str(operator_chat)
            )
            if pending is None:
                return False
            if pending.state not in ("waiting", "failed"):
                # Already redeeming — let the active redeem finish; if the
                # redeem succeeds, the row becomes `done` and a follow-up
                # token from the operator falls through to normal triage.
                return False
            state_module.transition(conn, pending_id=pending.id, new_state="redeeming")
        finally:
            conn.close()
        token = (event.content or "").strip()
        rc, stderr = self._redeem_token(token)
        fingerprint = state_module.fingerprint_token(token)
        if rc == 0:
            self._on_redeem_success(pending.id, fingerprint, operator_chat)
        else:
            self._on_redeem_failure(pending.id, fingerprint, operator_chat, rc, stderr)
        return True

    # --- redemption helpers ----------------------------------------------

    def _redeem_token(self, token: str) -> tuple[int, str]:
        """Pipe the token via stdin to `claude /login`. Token never appears in argv."""
        try:
            proc = subprocess.run(
                ["claude", "/login"],
                input=token,
                text=True,
                capture_output=True,
                timeout=_REDEEM_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            return 127, "claude binary not found on PATH"
        except subprocess.TimeoutExpired:
            return 124, "claude /login timed out (likely opened a browser)"
        except Exception as exc:  # noqa: BLE001
            return 1, f"redeem subprocess error: {exc}"
        return proc.returncode, (proc.stderr or proc.stdout or "").strip()

    def _on_redeem_success(self, pending_id: int, fingerprint: str, operator_chat) -> None:
        from .. import queue

        conn = queue.connect(self.runtime.instance_dir)
        try:
            updated = state_module.transition(conn, pending_id=pending_id, new_state="done")
            if updated is None:
                return
            event_ids = [updated.event_id, *updated.pending_events]
            for evt_id in event_ids:
                try:
                    queue.retry_now(conn, evt_id)
                except KeyError:
                    continue
        finally:
            conn.close()
        self.runtime.log(
            f"recovery redeem ok pending_id={pending_id} fingerprint={fingerprint} "
            f"replayed={len(event_ids)}",
            kind="recovery_redeem_ok",
        )
        self._dm_operator(operator_chat, f"✅ Re-auth ok ({fingerprint}) — replaying queued events.")

    def _on_redeem_failure(
        self,
        pending_id: int,
        fingerprint: str,
        operator_chat,
        rc: int,
        stderr: str,
    ) -> None:
        from .. import queue

        conn = queue.connect(self.runtime.instance_dir)
        try:
            state_module.transition(conn, pending_id=pending_id, new_state="failed")
        finally:
            conn.close()
        self.runtime.log(
            f"recovery redeem failed pending_id={pending_id} fingerprint={fingerprint} "
            f"rc={rc}",
            kind="recovery_redeem_failed",
        )
        snippet = (stderr or "").splitlines()[:3]
        self._dm_operator(
            operator_chat,
            f"⚠️ Re-auth failed ({fingerprint}) rc={rc}\n"
            + "\n".join(snippet)
            + "\n\nPaste a fresh token to retry.",
        )

    # --- helpers ----------------------------------------------------------

    def _make_context(self) -> RecoveryContext:
        return RecoveryContext(
            instance_dir=self.runtime.instance_dir,
            config=self.runtime.config,
            runtime=self.runtime,
            log=self.runtime.log,
        )

    def _chat_id_from_meta(self, event) -> str | None:
        if not event.meta:
            return None
        try:
            data = json.loads(event.meta)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        chat = data.get("chat_id")
        return str(chat) if chat is not None else None

    def _dm_operator(self, chat_id, body: str) -> None:
        try:
            from ..channels.telegram import TelegramChannel
            from ..config import ChannelConfig

            cfg = self.runtime.config.channels.get("telegram") or ChannelConfig()
            channel = TelegramChannel(self.runtime.instance_dir, cfg, self.runtime.log)
            if not channel.ready():
                return
            channel.send(body, {"chat_id": str(chat_id)})
        except Exception as exc:  # noqa: BLE001
            self.runtime.log(f"recovery dm error: {exc}")


def expire_pending_rows(instance_dir: Path) -> int:
    """Convenience helper for cron / supervisor: expire timed-out auth rows."""
    from .. import queue

    conn = queue.connect(instance_dir)
    try:
        expired = state_module.expire_old(conn)
    finally:
        conn.close()
    return len(expired)
