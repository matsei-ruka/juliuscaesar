"""Self-heal recovery — adapter rc!=0 classification + per-failure handlers.

Hooked into `GatewayRuntime.dispatch_once`. On `AdapterFailure`, the
dispatcher calls `RecoveryDispatcher.handle(event, failure)` which:

  1. Asks the classifier (cheap LLM via OpenRouter, with regex prefilter)
     what kind of failure this is.
  2. Looks up a `RecoveryHandler` for the classification.
  3. Returns a `RecoveryDecision` (Retry / Fail / Defer) the dispatcher acts on.

If the classifier outage or the handler errors, the runtime falls back to the
original blind retry behavior — recovery never makes things worse than the
prior contract.
"""

from .classifier import Classification, classify
from .dispatcher import RecoveryDispatcher, expire_pending_rows
from .handlers.base import (
    Defer,
    Fail,
    RecoveryContext,
    RecoveryDecision,
    RecoveryHandler,
    Retry,
)


__all__ = [
    "Classification",
    "Defer",
    "Fail",
    "RecoveryContext",
    "RecoveryDecision",
    "RecoveryDispatcher",
    "RecoveryHandler",
    "Retry",
    "classify",
    "expire_pending_rows",
]
