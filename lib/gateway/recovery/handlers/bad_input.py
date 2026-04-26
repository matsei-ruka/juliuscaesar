"""Bad-input handler — fail fast, no retry."""

from __future__ import annotations

from .base import Fail, RecoveryContext, RecoveryDecision


class BadInputHandler:
    """The event itself is wrong (oversize image, MIME mismatch, etc.).

    Retrying will not help. Mark failed and surface the classifier's reason.
    """

    def handle(self, event, classification, ctx: RecoveryContext) -> RecoveryDecision:
        reason = classification.extracted.get("reason") if isinstance(classification.extracted, dict) else None
        return Fail(reason=str(reason or "bad input — adapter rejected event"))
