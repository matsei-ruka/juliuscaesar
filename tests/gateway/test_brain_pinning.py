"""pin_to_default_brain routing behavior."""

from __future__ import annotations

from pathlib import Path

from gateway import router
from gateway.config import GatewayConfig
from gateway.queue import Event
from gateway.runtime import GatewayRuntime
from gateway.triage.base import TriageResult


def _event() -> Event:
    return Event(
        id=1,
        source="telegram",
        source_message_id="m1",
        user_id="u1",
        conversation_id="c1",
        content="hello",
        meta="{}",
        status="queued",
        received_at="2026-05-09T00:00:00Z",
        available_at="2026-05-09T00:00:00Z",
        locked_by=None,
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


def _runtime(tmp_path: Path, pin: bool) -> GatewayRuntime:
    instance = tmp_path / "instance"
    (instance / "ops").mkdir(parents=True)
    (instance / "ops" / "gateway.yaml").write_text(
        f"""
default_brain: claude:sonnet-4-6
pin_to_default_brain: {str(pin).lower()}
triage: always
triage_routing:
  quick: claude:haiku-4-5
""".lstrip(),
        encoding="utf-8",
    )
    return GatewayRuntime(
        instance,
        log_path=instance / "state" / "gateway" / "gateway.log",
        stop_requested=lambda: False,
    )


class QuickBackend:
    name = "fake"

    def classify(self, _message: str) -> TriageResult:
        return TriageResult(class_="quick", confidence=0.99)


class UnsafeBackend:
    name = "fake"

    def classify(self, _message: str) -> TriageResult:
        return TriageResult(class_="unsafe", confidence=0.99)


def test_pin_to_default_brain_ignores_non_unsafe_triage_hint(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, pin=True)
    runtime._get_triage_backend = lambda: QuickBackend()

    hint, should_reject = runtime._maybe_triage(_event(), None)

    assert hint is None
    assert should_reject is False


def test_default_unpinned_behavior_honors_triage_hint(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, pin=False)
    runtime._get_triage_backend = lambda: QuickBackend()

    hint, should_reject = runtime._maybe_triage(_event(), None)

    assert should_reject is False
    assert hint is not None
    assert hint.full_spec() == "claude:haiku-4-5"


def test_pin_to_default_brain_does_not_disable_unsafe_rejection(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, pin=True)
    runtime._get_triage_backend = lambda: UnsafeBackend()

    hint, should_reject = runtime._maybe_triage(_event(), None)

    assert hint is None
    assert should_reject is True


def test_pin_to_default_brain_forces_default_route_over_sticky_and_triage() -> None:
    cfg = GatewayConfig(
        default_brain="claude",
        default_model="sonnet-4-6",
        pin_to_default_brain=True,
    )
    triage = router.TriageHint(brain="opencode", model="deepseek-v4-flash", confidence=0.99)
    sticky = router.StickyHint(brain="codex", model="gpt-5")

    selection = router.route(
        _event(),
        cfg=cfg,
        sticky=sticky,
        triage=triage,
        confidence_threshold=0.7,
    )

    assert selection.brain == "claude"
    assert selection.model == "sonnet-4-6"
    assert selection.reason == "default_pinned"
