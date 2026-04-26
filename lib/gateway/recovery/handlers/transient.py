"""Transient failure handler — retry with the existing backoff schedule."""

from __future__ import annotations

from .base import RecoveryContext, RecoveryDecision, Retry


class TransientHandler:
    """Network blip / 5xx / timeout. Re-enqueue with a short delay."""

    DEFAULT_DELAY_SECONDS = 5.0

    def handle(self, event, classification, ctx: RecoveryContext) -> RecoveryDecision:
        # Use the gateway's configured backoff for the current retry slot if
        # available; otherwise the default 5 s. The runtime applies the delay.
        backoff = getattr(ctx.config.reliability, "backoff_seconds", None) or (5,)
        idx = min(int(getattr(event, "retry_count", 0) or 0), len(backoff) - 1)
        try:
            delay = float(backoff[idx])
        except (TypeError, ValueError, IndexError):
            delay = self.DEFAULT_DELAY_SECONDS
        return Retry(reason="transient adapter failure", delay_seconds=delay)
