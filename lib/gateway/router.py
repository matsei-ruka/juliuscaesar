"""Pure routing decision for the gateway.

`route()` is intentionally I/O-free so the decision tree is testable in
isolation. The runtime resolves sticky and triage *outside* `route()` and
passes the results in.

Decision order:

1. event.meta.brain_override        → use it (skips triage and sticky)
2. event.source == 'cron' + meta.brain → use the task's pinned brain
3. sticky brain (if provided)       → reuse it
4. triage result (if provided + confidence above threshold) → use it
5. fallback to per-channel default brain

Sprint 1 wires only steps 1, 2, and 5. Sprint 4 connects sticky and triage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .config import GatewayConfig
from .queue import Event


@dataclass(frozen=True)
class BrainSelection:
    brain: str
    model: str | None
    reason: str


@dataclass(frozen=True)
class StickyHint:
    brain: str
    model: str | None = None


@dataclass(frozen=True)
class TriageHint:
    brain: str
    model: str | None
    confidence: float


def _decode_meta(event: Event) -> dict:
    if not event.meta:
        return {}
    try:
        data = json.loads(event.meta)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _split_brain_spec(spec: str) -> tuple[str, str | None]:
    """`claude:opus-4-7-1m` → ('claude', 'opus-4-7-1m'); `codex` → ('codex', None)."""
    if not spec:
        return "", None
    if ":" in spec:
        brain, model = spec.split(":", 1)
        return brain, model or None
    return spec, None


def channel_name(event: Event) -> str:
    """Resolve the logical channel for routing/delivery purposes."""
    meta = _decode_meta(event)
    if event.source in ("telegram", "slack", "discord", "voice"):
        return event.source
    return str(meta.get("channel") or event.source)


def route(
    event: Event,
    *,
    cfg: GatewayConfig,
    sticky: StickyHint | None = None,
    triage: TriageHint | None = None,
    confidence_threshold: float = 0.7,
    fallback_brain: str | None = None,
) -> BrainSelection:
    meta = _decode_meta(event)

    override = meta.get("brain_override")
    if isinstance(override, str) and override.strip():
        brain, model = _split_brain_spec(override.strip())
        return BrainSelection(brain=brain, model=model, reason="brain_override")

    if event.source == "cron":
        cron_brain = meta.get("brain")
        if isinstance(cron_brain, str) and cron_brain.strip():
            brain, model_from_meta = _split_brain_spec(cron_brain.strip())
            model = (
                str(meta["model"])
                if isinstance(meta.get("model"), str) and meta["model"]
                else model_from_meta
            )
            return BrainSelection(brain=brain, model=model, reason="cron_pinned")

    if sticky is not None and sticky.brain:
        return BrainSelection(brain=sticky.brain, model=sticky.model, reason="sticky")

    if triage is not None and triage.confidence >= confidence_threshold:
        return BrainSelection(brain=triage.brain, model=triage.model, reason="triage")

    if triage is not None and fallback_brain:
        brain, model = _split_brain_spec(fallback_brain)
        return BrainSelection(brain=brain, model=model, reason="triage_fallback")

    channel = channel_name(event)
    brain, model = cfg.brain_for(channel)
    return BrainSelection(brain=brain, model=model, reason="channel_default")
