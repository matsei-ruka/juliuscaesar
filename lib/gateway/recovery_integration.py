"""Runtime integration for adapter-failure recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import queue
from .recovery import Defer, Fail, Retry

if TYPE_CHECKING:
    from .brains import AdapterFailure
    from .queue import Event


class RecoveryIntegration:
    """Bridges `GatewayRuntime` queue handling to `RecoveryDispatcher` decisions."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.dispatcher = self._build_dispatcher()

    def _build_dispatcher(self):
        try:
            from .recovery import RecoveryDispatcher

            return RecoveryDispatcher(self.runtime)
        except Exception as exc:  # noqa: BLE001
            self.runtime.log(f"recovery dispatcher unavailable: {exc}")
            return None

    def maybe_consume_auth_token(self, event: "Event") -> bool:
        if self.dispatcher is None:
            return False
        return bool(self.dispatcher.maybe_consume_auth_token(event))

    def handle_adapter_failure(self, event: "Event", failure: "AdapterFailure") -> None:
        """Route an adapter rc!=0 through recovery, falling back to blind retry."""
        if self.dispatcher is None:
            self.fallback_blind_retry(event, str(failure))
            return
        try:
            decision = self.dispatcher.handle(event, failure)
        except Exception as exc:  # noqa: BLE001
            self.runtime.log(
                f"recovery dispatcher error id={event.id} brain={failure.brain} "
                f"reason={exc!r} — falling back to blind retry"
            )
            self.fallback_blind_retry(event, str(failure))
            return
        if isinstance(decision, Retry):
            conn = queue.connect(self.runtime.instance_dir)
            try:
                failed = queue.fail(
                    conn,
                    event.id,
                    error=f"recovery: retry ({decision.reason})"[:1000],
                    max_retries=self.runtime.config.max_retries,
                    backoff_seconds=(int(decision.delay_seconds),),
                )
            finally:
                conn.close()
            self.runtime.log(
                f"recovery retry id={event.id} brain={failure.brain} "
                f"delay={decision.delay_seconds}s status={failed.status}"
            )
        elif isinstance(decision, Fail):
            conn = queue.connect(self.runtime.instance_dir)
            try:
                queue.fail(
                    conn,
                    event.id,
                    error=f"recovery: {decision.reason}"[:1000],
                    max_retries=0,
                )
            finally:
                conn.close()
            self.runtime.log(
                f"recovery fail id={event.id} brain={failure.brain} reason={decision.reason}"
            )
        elif isinstance(decision, Defer):
            self.runtime.log(
                f"recovery defer id={event.id} brain={failure.brain} reason={decision.reason}"
            )
        else:
            self.runtime.log(
                f"recovery unknown decision id={event.id} "
                f"type={type(decision).__name__} — failing event"
            )
            conn = queue.connect(self.runtime.instance_dir)
            try:
                queue.fail(conn, event.id, error="recovery: unknown decision", max_retries=0)
            finally:
                conn.close()

    def fallback_blind_retry(self, event: "Event", error: str) -> None:
        conn = queue.connect(self.runtime.instance_dir)
        try:
            failed = queue.fail(
                conn,
                event.id,
                error=error[:1000],
                max_retries=self.runtime.config.max_retries,
            )
        finally:
            conn.close()
        self.runtime.log(f"event {failed.status} id={event.id} error={error}")
