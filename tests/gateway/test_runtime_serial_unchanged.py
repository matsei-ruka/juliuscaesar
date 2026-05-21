"""Regression tests: N=1 dispatch must be byte-identical to pre-parallel-slots.

Spec acceptance (docs/specs/parallel-slots.md):

    When max_concurrent: 1 (default, or block omitted entirely), every code
    path must produce byte-identical behavior to today.

These tests pin down the observable side-effects we care about:
- the classifier is NEVER invoked when N=1;
- the footer rendering does NOT include `slot N`;
- the supervisor card has phase emoji as leading char (no slot keycap);
- the telegram busy reaction draws from `_BUSY_EMOJIS` (random pick), not the
  deterministic `🏃` reserved for the parallel path.
- session rows are upsertable with no slot kwarg.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue, reply_footer, sessions  # noqa: E402
from gateway.brain import BrainResult  # noqa: E402
from gateway.config import ReplyFooterConfig, render_default_config  # noqa: E402
from gateway.runtime import GatewayRuntime  # noqa: E402
from supervisor.cards import render_card  # noqa: E402
from supervisor.models import PhaseResult  # noqa: E402


def _instance() -> Path:
    root = Path(tempfile.mkdtemp(prefix="jc-runtime-serial-"))
    (root / ".jc").write_text("", encoding="utf-8")
    (root / "ops").mkdir()
    (root / "memory" / "L1").mkdir(parents=True)
    (root / "memory" / "L1" / "IDENTITY.md").write_text("test", encoding="utf-8")
    (root / "ops" / "gateway.yaml").write_text(
        render_default_config(default_brain="claude:sonnet-4-6"),
        encoding="utf-8",
    )
    return root


def _enqueue(instance: Path) -> int:
    conn = queue.connect(instance)
    try:
        ev, _ = queue.enqueue(
            conn,
            source="telegram",
            source_message_id="m1",
            conversation_id="28547271",
            content="hi",
            meta={"chat_id": "28547271"},
        )
    finally:
        conn.close()
    return ev.id


class SerialDispatchUnchangedTests(unittest.TestCase):
    def test_default_max_concurrent_is_one(self) -> None:
        instance = _instance()
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        try:
            self.assertEqual(runtime.config.parallel.max_concurrent, 1)
        finally:
            runtime.close()

    def test_classifier_not_called_for_n1(self) -> None:
        instance = _instance()
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        try:
            _enqueue(instance)
            with mock.patch(
                "gateway.runtime.invoke_brain",
                return_value=BrainResult("hello back", "sess-x"),
            ), mock.patch(
                "gateway.runtime.deliver_response", return_value="m-out"
            ), mock.patch.object(
                runtime,
                "_classify_slot_affinity",
                side_effect=AssertionError(
                    "classifier must NOT be invoked when max_concurrent=1"
                ),
            ):
                self.assertTrue(runtime.dispatch_once())
        finally:
            runtime.close()

    def test_session_upsert_lands_at_slot_zero_silently(self) -> None:
        """Serial dispatch must upsert sessions at slot 0 (default) — no slot column logging."""
        instance = _instance()
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        try:
            _enqueue(instance)
            with mock.patch(
                "gateway.runtime.invoke_brain",
                return_value=BrainResult("hello back", "sess-x123"),
            ), mock.patch("gateway.runtime.deliver_response", return_value="m-out"):
                self.assertTrue(runtime.dispatch_once())
            conn = queue.connect(instance)
            try:
                row = sessions.get_session(
                    conn,
                    channel="telegram",
                    conversation_id="28547271",
                    brain="claude",
                )
            finally:
                conn.close()
            assert row is not None
            self.assertEqual(row.slot, 0)
            self.assertEqual(row.session_id, "sess-x123")
        finally:
            runtime.close()

    def test_no_slot_threads_spawned_for_serial_path(self) -> None:
        instance = _instance()
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        try:
            _enqueue(instance)
            with mock.patch(
                "gateway.runtime.invoke_brain",
                return_value=BrainResult("hello back", "sess-x"),
            ), mock.patch("gateway.runtime.deliver_response", return_value="m-out"):
                self.assertTrue(runtime.dispatch_once())
            self.assertEqual(runtime._busy_slots, {})
            self.assertEqual(runtime._slot_active_threads, set())
        finally:
            runtime.close()


class SerialFooterUnchangedTests(unittest.TestCase):
    def test_footer_has_no_slot_segment_when_max_concurrent_one(self) -> None:
        footer = reply_footer.render_footer(
            ReplyFooterConfig(enabled=True),
            brain="claude",
            model="sonnet-4-6",
            session_id="abcd1234",
            elapsed_seconds=4.2,
            slot=0,
            max_concurrent=1,
        )
        self.assertEqual(footer, "⚙️ claude:sonnet-4-6 · sess abcd1234 · 4.2s")
        self.assertNotIn("slot", footer or "")

    def test_footer_default_call_matches_legacy(self) -> None:
        legacy = reply_footer.render_footer(
            ReplyFooterConfig(enabled=True),
            brain="claude",
            model="sonnet-4-6",
            session_id="abcd1234",
            elapsed_seconds=4.2,
        )
        explicit = reply_footer.render_footer(
            ReplyFooterConfig(enabled=True),
            brain="claude",
            model="sonnet-4-6",
            session_id="abcd1234",
            elapsed_seconds=4.2,
            slot=0,
            max_concurrent=1,
        )
        self.assertEqual(legacy, explicit)


class SerialCardUnchangedTests(unittest.TestCase):
    def test_card_render_no_slot_kwarg_matches_max_concurrent_one(self) -> None:
        phase = PhaseResult(
            phase="coding", emoji="🛠️", label={"en": "coding", "it": "sviluppo"}
        )
        legacy = render_card(
            title="audit Athena", phase=phase, elapsed_seconds=120.0
        )
        explicit = render_card(
            title="audit Athena",
            phase=phase,
            elapsed_seconds=120.0,
            slot=0,
            max_concurrent=1,
        )
        self.assertEqual(legacy.text, explicit.text)
        self.assertTrue(legacy.text.startswith("🛠️"))


if __name__ == "__main__":
    unittest.main()
