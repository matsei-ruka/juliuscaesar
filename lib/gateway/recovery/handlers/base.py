"""Recovery handler ABC + decision dataclasses."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Union

if TYPE_CHECKING:
    from ...config import GatewayConfig
    from ...queue import Event
    from ..classifier import Classification


@dataclass(frozen=True)
class Retry:
    reason: str
    delay_seconds: float = 0.0


@dataclass(frozen=True)
class Fail:
    reason: str


@dataclass(frozen=True)
class Defer:
    reason: str


RecoveryDecision = Union[Retry, Fail, Defer]


@dataclass
class RecoveryContext:
    """Mutable context passed into every handler.

    Carries the runtime, instance dir, gateway config, and a `log` callable so
    handlers don't need to reach back into the runtime for routine ops.
    """

    instance_dir: Path
    config: "GatewayConfig"
    runtime: object
    log: object  # callable(msg: str, **fields) → None


class RecoveryHandler(Protocol):
    """Protocol for recovery handlers — duck-typed.

    Each handler implements `handle(event, classification, ctx) → decision`.
    Handlers must be threadsafe (they may run concurrently with the next
    dispatch tick) and idempotent under retry.
    """

    def handle(
        self,
        event: "Event",
        classification: "Classification",
        ctx: RecoveryContext,
    ) -> RecoveryDecision:
        ...
