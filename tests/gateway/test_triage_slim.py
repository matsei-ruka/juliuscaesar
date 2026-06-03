"""Regression coverage for the slim triage output contract."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
import json
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.config import (  # noqa: E402
    DEFAULT_TRIAGE_ROUTING,
    GatewayConfig,
    TriageConfig,
    load_config,
)
from gateway.queue import Event  # noqa: E402
from gateway.runtime import GatewayRuntime  # noqa: E402
from gateway.triage import MetricsRecorder  # noqa: E402
from gateway.triage.base import TriageResult, parse_triage_json  # noqa: E402
from gateway.triage.factory import build_backend  # noqa: E402


def _runtime_with_triage(cfg: TriageConfig):
    return types.SimpleNamespace(
        config=GatewayConfig(triage=cfg),
        _brain_failure=types.SimpleNamespace(is_failed=lambda _brain: False),
        log=lambda *_args, **_kwargs: None,
    )


def _event(content: str = "bad request", meta: dict | None = None) -> Event:
    return Event(
        id=1,
        source="telegram",
        source_message_id="m1",
        user_id="u1",
        conversation_id="c1",
        content=content,
        meta=json.dumps(meta) if meta else None,
        status="queued",
        received_at="2026-05-07T00:00:00Z",
        available_at="2026-05-07T00:00:00Z",
        locked_by=None,
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


class SlimTriageContractTests(unittest.TestCase):
    def test_v2_json_parses_without_brain(self):
        result = parse_triage_json('{"class":"quick","confidence":0.9}')

        self.assertEqual(result, TriageResult(class_="quick", confidence=0.9, raw=result.raw))
        self.assertFalse(hasattr(result, "brain"))

    def test_v1_json_brain_is_accepted_and_discarded(self):
        result = parse_triage_json(
            '{"class":"quick","brain":"claude:opus-4-7-1m","confidence":0.9}'
        )

        self.assertEqual(result.class_, "quick")
        self.assertAlmostEqual(result.confidence, 0.9)
        self.assertFalse(hasattr(result, "brain"))

    def test_missing_class_or_confidence_rejects(self):
        self.assertIsNone(parse_triage_json('{"confidence":0.9}'))
        self.assertIsNone(parse_triage_json('{"class":"quick"}'))

    def test_triage_to_hint_uses_routing_map(self):
        cfg = TriageConfig(
            routing={"code": "claude:sonnet-4-6"},
            fallback_brain="claude:haiku-4-5",
        )
        runtime = _runtime_with_triage(cfg)

        hint = GatewayRuntime._triage_to_hint(
            runtime,
            TriageResult(class_="code", confidence=0.93),
        )

        self.assertEqual(hint.brain, "claude")
        self.assertEqual(hint.model, "sonnet-4-6")
        self.assertEqual(hint.full_spec(), "claude:sonnet-4-6")

    def test_triage_to_hint_falls_back_when_class_unmapped(self):
        cfg = TriageConfig(routing={}, fallback_brain="claude:sonnet-4-6")
        runtime = _runtime_with_triage(cfg)

        hint = GatewayRuntime._triage_to_hint(
            runtime,
            TriageResult(class_="analysis", confidence=0.8),
        )

        self.assertEqual(hint.brain, "claude")
        self.assertEqual(hint.model, "sonnet-4-6")

    def test_triage_to_hint_can_decline_when_unmapped_and_no_fallback(self):
        cfg = TriageConfig(routing={}, fallback_brain="")
        runtime = _runtime_with_triage(cfg)

        hint = GatewayRuntime._triage_to_hint(
            runtime,
            TriageResult(class_="analysis", confidence=0.8),
        )

        self.assertIsNone(hint)

    def test_config_loader_ships_default_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "ops").mkdir()
            (instance / "ops" / "gateway.yaml").write_text("triage: always\n", encoding="utf-8")

            cfg = load_config(instance)

            self.assertEqual(cfg.triage.routing, DEFAULT_TRIAGE_ROUTING)

    def test_config_loader_overrides_default_routing_per_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "ops").mkdir()
            (instance / "ops" / "gateway.yaml").write_text(
                "triage: always\n"
                "triage_routing:\n"
                "  code: codex:gpt-5.4\n",
                encoding="utf-8",
            )

            cfg = load_config(instance)

            self.assertEqual(cfg.triage.routing["code"], "codex:gpt-5.4")
            self.assertEqual(cfg.triage.routing["analysis"], DEFAULT_TRIAGE_ROUTING["analysis"])

    def test_none_backend_returns_slim_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = build_backend(TriageConfig(backend="none"), Path(tmp))

            result = backend.classify("hello")

            self.assertEqual(result.class_, "quick")
            self.assertEqual(result.confidence, 0.0)
            self.assertFalse(hasattr(result, "brain"))

    def test_metrics_record_uses_routed_brain_parameter(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = MetricsRecorder(Path(tmp))

            recorder.record(TriageResult("code", 0.95), brain="claude:sonnet-4-6")

            conn = sqlite3.connect(recorder.path)
            try:
                row = conn.execute("SELECT class_, brain, confidence FROM observations").fetchone()
            finally:
                conn.close()

            self.assertEqual(row, ("code", "claude:sonnet-4-6", 0.95))

    def test_unsafe_class_still_rejects(self):
        result = parse_triage_json('{"class":"unsafe","confidence":1.0}')

        self.assertTrue(result.is_unsafe())

    def test_unsafe_triage_result_notifies_sender_without_invoking_brain(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "ops").mkdir()
            (instance / "ops" / "gateway.yaml").write_text(
                "triage: always\n",
                encoding="utf-8",
            )
            runtime = GatewayRuntime(
                instance,
                log_path=instance / "state" / "gateway" / "gateway.log",
                stop_requested=lambda: False,
            )

            class UnsafeBackend:
                name = "fake"

                def classify(self, message):
                    return TriageResult(class_="unsafe", confidence=1.0)

            runtime._get_triage_backend = lambda: UnsafeBackend()
            delivered = []
            runtime._deliver_response = lambda source, response, meta: delivered.append(
                (source, response, meta)
            )
            with mock.patch("gateway.runtime.invoke_brain") as invoke:
                response = runtime.process_event(_event())

            self.assertIn("safety policy", response)
            self.assertEqual(len(delivered), 1)
            self.assertEqual(delivered[0][0], "telegram")
            self.assertIn("safety policy", delivered[0][1])
            self.assertEqual(delivered[0][2]["delivery_channel"], "telegram")
            invoke.assert_not_called()

    def test_voice_event_bypasses_classifier_and_uses_voice_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "ops").mkdir()
            (instance / "ops" / "gateway.yaml").write_text(
                "triage: openrouter\n"
                "triage_routing:\n"
                "  voice: claude:sonnet\n",
                encoding="utf-8",
            )
            runtime = GatewayRuntime(
                instance,
                log_path=instance / "state" / "gateway" / "gateway.log",
                stop_requested=lambda: False,
            )
            event = _event("Hello Ada", meta={"was_voice": True})

            class Classifier:
                def classify(self, _message):
                    raise AssertionError("voice events should not call triage backend")

            runtime._get_triage_backend = lambda: Classifier()

            hint, should_reject = runtime._maybe_triage(event, None)

            self.assertFalse(should_reject)
            self.assertIsNotNone(hint)
            self.assertEqual(hint.full_spec(), "claude:sonnet")


if __name__ == "__main__":
    unittest.main()
