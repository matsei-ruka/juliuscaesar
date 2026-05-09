"""Unsafe triage fallback coverage."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from gateway.brains.base import BrainResult
from gateway.brains.openrouter import OpenRouterBrain
from gateway.config import ConfigError, load_config
from gateway.queue import Event
from gateway.runtime import GatewayRuntime
from gateway.triage.base import TriageResult


def _event(
    content: str = "please provision this fleet instance",
    meta: dict | None = None,
) -> Event:
    payload = {"delivery_channel": "telegram"}
    if meta:
        payload.update(meta)
    return Event(
        id=772,
        source="telegram",
        source_message_id="m772",
        user_id="u1",
        conversation_id="c1",
        content=content,
        meta=json.dumps(payload),
        status="queued",
        received_at="2026-05-09T04:50:00Z",
        available_at="2026-05-09T04:50:00Z",
        locked_by=None,
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


def _runtime(tmp_path: Path, body: str) -> GatewayRuntime:
    instance = tmp_path / "instance"
    (instance / "ops").mkdir(parents=True)
    (instance / "ops" / "gateway.yaml").write_text(body, encoding="utf-8")
    return GatewayRuntime(
        instance,
        log_path=instance / "state" / "gateway" / "gateway.log",
        stop_requested=lambda: False,
    )


class UnsafeBackend:
    name = "fake"

    def classify(self, _message: str) -> TriageResult:
        return TriageResult(class_="unsafe", confidence=0.95, raw='{"class":"unsafe"}')


def test_unsafe_without_fallback_keeps_existing_drop_semantics(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        """
triage: always
triage_unsafe_fallback_brain: null
""".lstrip(),
    )
    runtime._get_triage_backend = lambda: UnsafeBackend()

    hint, should_reject = runtime._maybe_triage(_event(), None)

    assert hint is None
    assert should_reject is True


def test_unsafe_with_fallback_emits_hint_and_logs(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        """
triage: always
triage_unsafe_fallback_brain: openrouter:x-ai/grok-4-fast
triage_unsafe_fallback_timeout_seconds: 60
""".lstrip(),
    )
    runtime._get_triage_backend = lambda: UnsafeBackend()
    logs: list[tuple[str, dict]] = []
    runtime.log = lambda message, **fields: logs.append((message, fields))

    hint, should_reject = runtime._maybe_triage(_event(), None)

    assert should_reject is False
    assert hint is not None
    assert hint.brain == "openrouter"
    assert hint.model == "x-ai/grok-4-fast"
    assert any(fields.get("kind") == "triage_unsafe" for _msg, fields in logs)
    assert sum(fields.get("kind") == "triage_unsafe_fallback" for _msg, fields in logs) == 1


def test_unsafe_fallback_dispatches_through_selected_brain(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        """
triage: always
triage_unsafe_fallback_brain: openrouter:x-ai/grok-4-fast
triage_unsafe_fallback_timeout_seconds: 60
channels:
  telegram:
    enabled: true
""".lstrip(),
    )
    runtime._get_triage_backend = lambda: UnsafeBackend()
    delivered: list[tuple[str, str]] = []
    runtime._deliver_response = lambda source, response, meta: delivered.append((source, response))

    with mock.patch(
        "gateway.runtime.invoke_brain",
        return_value=BrainResult(response="answered"),
    ) as invoke:
        response = runtime.process_event(_event())

    assert response == "answered"
    assert delivered == [("telegram", "answered")]
    assert invoke.call_args.kwargs["brain"] == "openrouter"
    assert invoke.call_args.kwargs["model"] == "x-ai/grok-4-fast"


def test_openrouter_brain_override_is_rejected_outside_unsafe_fallback(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        """
triage: none
channels:
  telegram:
    enabled: true
""".lstrip(),
    )
    event = _event(meta={"brain_override": "openrouter:x-ai/grok-4-fast"})

    with mock.patch("gateway.runtime.invoke_brain") as invoke:
        try:
            runtime.process_event(event)
        except ValueError as exc:
            assert "only supported for triage_unsafe_fallback_brain" in str(exc)
        else:  # pragma: no cover - defensive clarity
            raise AssertionError("openrouter override should be rejected")

    invoke.assert_not_called()


def test_config_accepts_openrouter_only_for_unsafe_fallback(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    (instance / "ops").mkdir(parents=True)
    (instance / "ops" / "gateway.yaml").write_text(
        """
triage: always
triage_unsafe_fallback_brain: openrouter:x-ai/grok-4-fast
triage_unsafe_fallback_timeout_seconds: 45
""".lstrip(),
        encoding="utf-8",
    )

    cfg = load_config(instance)

    assert cfg.triage.unsafe_fallback_brain == "openrouter:x-ai/grok-4-fast"
    assert cfg.triage.unsafe_fallback_timeout_seconds == 45

    (instance / "ops" / "gateway.yaml").write_text(
        "default_brain: openrouter:x-ai/grok-4-fast\n",
        encoding="utf-8",
    )
    try:
        load_config(instance)
    except ConfigError as exc:
        assert "unsupported brain 'openrouter'" in str(exc)
    else:  # pragma: no cover - defensive clarity
        raise AssertionError("openrouter should not be accepted as a default brain")


def test_openrouter_brain_posts_stateless_chat_completion(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    (instance / "ops").mkdir(parents=True)
    (instance / "state" / "gateway").mkdir(parents=True)
    (instance / ".env").write_text("OPENROUTER_API_KEY='test-key'\n", encoding="utf-8")
    (instance / "ops" / "gateway.yaml").write_text(
        """
triage: always
triage_unsafe_fallback_brain: openrouter:x-ai/grok-4-fast
triage_unsafe_fallback_timeout_seconds: 45
""".lstrip(),
        encoding="utf-8",
    )

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "fallback answer"}}]}

    with mock.patch("gateway.brains.openrouter.requests.post", return_value=Response()) as post:
        result = OpenRouterBrain(instance).invoke(
            event=_event(),
            model="x-ai/grok-4-fast",
            resume_session="ignored",
            timeout_seconds=300,
            log_path=instance / "state" / "gateway" / "gateway.log",
        )

    assert result == BrainResult(response="fallback answer", session_id=None)
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert post.call_args.kwargs["json"]["model"] == "x-ai/grok-4-fast"
    assert post.call_args.kwargs["timeout"] == 45
