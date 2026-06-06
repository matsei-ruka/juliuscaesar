"""Runtime coverage for context-aware session lifecycle routing."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from gateway import queue, sessions
from gateway.brains.base import BrainResult
from gateway.lifecycle import compaction, routing, telemetry
from gateway.runtime import GatewayRuntime


def _config(*, default_brain: str = "claude:small", model_name: str = "small", extended_enabled: bool = True) -> str:
    return (
        f"""
default_brain: {default_brain}
channels:
  telegram:
    enabled: true
session_lifecycle:
  enabled: true
  thresholds:
    observe_ratio: 0.50
    idle_maintenance_ratio: 0.60
    rotate_ratio: 0.70
    emergency_ratio: 0.85
  reserves:
    output_tokens: 16000
    turn_input_tokens: 12000
  model_profiles:
    small-standard:
      model: {model_name}
      input_capacity_tokens: 100000
    small-extended:
      model: {model_name}
      variant: extended
      input_capacity_tokens: 300000
      extended_context: true
      enabled: {str(extended_enabled).lower()}
""".lstrip()
    )


def _instance(*, config_text: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix="jc-lifecycle-runtime-"))
    (root / ".jc").write_text("", encoding="utf-8")
    (root / "ops").mkdir()
    (root / "ops" / "gateway.yaml").write_text(config_text, encoding="utf-8")
    return root


def _runtime(instance: Path) -> GatewayRuntime:
    return GatewayRuntime(
        instance,
        log_path=instance / "state" / "gateway" / "gateway.log",
        stop_requested=lambda: False,
    )


def _event(*, event_id: int = 1, content: str = "hello", conv: str = "c1"):
    return queue.Event(
        id=event_id,
        source="telegram",
        source_message_id=f"m{event_id}",
        user_id="u1",
        conversation_id=conv,
        content=content,
        meta=json.dumps({"delivery_channel": "telegram", "chat_id": "123"}),
        status="queued",
        received_at="2026-06-06T00:00:00Z",
        available_at="2026-06-06T00:00:00Z",
        locked_by=None,
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


def _seed_session(instance: Path, *, brain: str = "claude", model: str = "small", tokens: int) -> None:
    conn = queue.connect(instance)
    try:
        sessions.upsert_session(
            conn,
            channel="telegram",
            conversation_id="c1",
            brain=brain,
            session_id="sess-1234",
            slot=0,
        )
        telemetry.record_usage(
            conn,
            owner_key=compaction.owner_key("telegram", "c1", brain, 0),
            brain=brain,
            usage=telemetry.ContextUsage.from_anthropic_usage({"input_tokens": tokens}),
            model=model,
            context_profile="small-standard",
        )
    finally:
        conn.close()


def _seed_rotated_session(instance: Path) -> None:
    conn = queue.connect(instance)
    try:
        sessions.upsert_session(
            conn,
            channel="telegram",
            conversation_id="c1",
            brain="claude",
            session_id="sess-1234",
            slot=0,
        )
        telemetry.record_usage(
            conn,
            owner_key=compaction.owner_key("telegram", "c1", "claude", 0),
            brain="claude",
            usage=telemetry.ContextUsage.from_anthropic_usage({"input_tokens": 10}),
            model="small",
            context_profile="small-standard",
        )
        telemetry.record_rotation(
            conn,
            owner_key=compaction.owner_key("telegram", "c1", "claude", 0),
        )
    finally:
        conn.close()


def test_rotate_pressure_dispatches_without_resume(tmp_path: Path) -> None:
    instance = _instance(config_text=_config())
    _seed_session(instance, tokens=220_000)
    runtime = _runtime(instance)
    runtime._deliver_response = lambda source, response, meta: None

    with mock.patch(
        "gateway.runtime.invoke_brain",
        return_value=BrainResult(response="fresh reply", session_id="sess-new"),
    ) as invoke:
        response = runtime.process_event(_event())

    assert response == "fresh reply"
    assert invoke.call_args.kwargs["resume_session"] is None
    runtime.close()


def test_fail_pressure_delivers_operator_error_and_skips_dispatch(tmp_path: Path) -> None:
    instance = _instance(config_text=_config(extended_enabled=False))
    _seed_session(instance, tokens=10_000)
    conn = queue.connect(instance)
    try:
        queue.enqueue(
            conn,
            source="telegram",
            source_message_id="m-fail",
            conversation_id="c1",
            content="x" * 400_000,
            meta={"chat_id": "123"},
        )
    finally:
        conn.close()
    runtime = _runtime(instance)
    delivered: list[str] = []
    runtime._deliver_response = lambda source, response, meta: delivered.append(response)

    with mock.patch("gateway.runtime.invoke_brain") as invoke:
        assert runtime.dispatch_once() is True

    assert delivered
    assert "Unable to route this turn safely" in delivered[-1]
    invoke.assert_not_called()
    conn = queue.connect(instance)
    try:
        row = conn.execute("SELECT status, error FROM events ORDER BY id DESC LIMIT 1").fetchone()
        assert row["status"] == "failed"
        assert "Unable to route this turn safely" in row["error"]
    finally:
        conn.close()
    runtime.close()


def test_usage_known_false_after_rotation(tmp_path: Path) -> None:
    instance = _instance(config_text=_config())
    _seed_rotated_session(instance)
    runtime = _runtime(instance)
    runtime._deliver_response = lambda source, response, meta: None

    with mock.patch(
        "gateway.runtime.routing.evaluate_pressure",
        wraps=routing.evaluate_pressure,
    ) as evaluate, mock.patch(
        "gateway.runtime.invoke_brain",
        return_value=BrainResult(response="ok", session_id="sess-new"),
    ):
        runtime.process_event(_event())

    assert evaluate.call_args.kwargs["usage_known"] is False
    runtime.close()


def test_upgrade_cross_family_rotates_instead(tmp_path: Path) -> None:
    instance = _instance(config_text=_config(default_brain="codex:claude-small", model_name="claude-small"))
    _seed_session(instance, brain="codex", model="claude-small", tokens=10_000)
    runtime = _runtime(instance)
    runtime._deliver_response = lambda source, response, meta: None

    with mock.patch(
        "gateway.runtime.invoke_brain",
        return_value=BrainResult(response="fresh reply", session_id="sess-new"),
    ) as invoke:
        runtime.process_event(_event(content="x" * 300_000))

    assert invoke.call_args.kwargs["resume_session"] is None
    runtime.close()


def test_upgrade_and_usage_logs_are_enriched(tmp_path: Path) -> None:
    instance = _instance(config_text=_config())
    _seed_session(instance, tokens=20_000)
    runtime = _runtime(instance)
    runtime._deliver_response = lambda source, response, meta: None
    logs: list[tuple[str, dict]] = []
    runtime.log = lambda message, **fields: logs.append((message, fields))

    with mock.patch(
        "gateway.runtime.invoke_brain",
        return_value=BrainResult(
            response="ok",
            session_id="sess-new",
            usage={"input_tokens": 42_000, "output_tokens": 200},
        ),
    ):
        runtime.process_event(_event(event_id=2, content="x" * 300_000))

    upgrade = next(fields for _msg, fields in logs if fields.get("kind") == "context_capacity_upgrade")
    assert upgrade["from_profile"] == "small-standard"
    assert upgrade["to_profile"] == "small-extended"
    assert upgrade["pressure"] > 1.0

    usage = next(fields for _msg, fields in logs if fields.get("kind") == "context_usage_updated")
    assert usage["brain"] == "claude"
    assert usage["slot"] == 0
    assert usage["model"] == "small"
    assert usage["effective_input_tokens"] == 42_000
    assert usage["lifecycle_pressure"] is not None
    runtime.close()
