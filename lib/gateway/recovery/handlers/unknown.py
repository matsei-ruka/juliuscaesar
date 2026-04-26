"""Unknown-failure handler — one retry, then fail."""

from __future__ import annotations

from .base import Fail, RecoveryContext, RecoveryDecision, Retry


class UnknownHandler:
    """Classifier didn't recognize the failure.

    One retry (in case it was a flaky one-off), then fail-fast so we don't
    burn resources on a sustained mystery error.
    """

    DELAY_SECONDS = 10.0

    def handle(self, event, classification, ctx: RecoveryContext) -> RecoveryDecision:
        retries = int(getattr(event, "retry_count", 0) or 0)
        if retries >= 1:
            return Fail(reason="unknown adapter failure (retries exhausted)")
        return Retry(reason="unknown adapter failure — one retry", delay_seconds=self.DELAY_SECONDS)
