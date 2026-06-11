"""Regression tests: N=1 dispatch must be byte-identical to pre-parallel-slots.

Spec acceptance (docs/specs/parallel-slots.md):

    When max_concurrent: 1 (default, or block omitted entirely), every code
    path must produce byte-identical behavior to today.

These tests pin down the observable side-effects we care about:
- the parallel slot path is NEVER engaged when N=1;
- the footer rendering does NOT include `slot N`;
- the supervisor card has phase emoji as leading char (no slot keycap);
- the telegram busy reaction draws from `_BUSY_EMOJIS` (random pick), not the
  deterministic `⚡` reserved for the parallel path.
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

    def test_parallel_slot_path_not_engaged_for_n1(self) -> None:
        # Was `test_classifier_not_called_for_n1`. The relatedness classifier
        # is deleted (docs/specs/deterministic-slot-routing.md), so the
        # invariant is now strictly stronger: the parallel dispatch path
        # itself must never be engaged when max_concurrent=1.
        instance = _instance()
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        try:
            self.assertFalse(hasattr(runtime, "_classify_slot_affinity"))
            _enqueue(instance)
            with mock.patch(
                "gateway.runtime.invoke_brain",
                return_value=BrainResult("hello back", "sess-x"),
            ), mock.patch(
                "gateway.runtime.deliver_response", return_value="m-out"
            ), mock.patch.object(
                runtime,
                "_dispatch_parallel",
                side_effect=AssertionError(
                    "parallel slot dispatch must NOT be engaged when max_concurrent=1"
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


class DispatchClaimPoisonEscalationTests(unittest.TestCase):
    """Phase 3 wire-up: config.max_retries must reach the inline requeue in
    the claim path, so dispatch_once escalates an expired poison row to
    `failed` instead of re-claiming it in the same transaction."""

    def _poison_row(self, instance: Path, *, retry_count: int) -> int:
        conn = queue.connect(instance)
        try:
            cur = conn.execute(
                """
                INSERT INTO events
                  (source, content, status, received_at, available_at,
                   started_at, locked_by, locked_until, retry_count,
                   conversation_id)
                VALUES ('telegram', 'poison', 'running', '2026-05-17T10:00:00Z',
                        '2026-05-17T10:00:00Z', '2026-05-17T10:00:00Z',
                        'worker-1#deadbeef', '2026-05-17T10:05:00Z', ?, '28547271')
                """,
                (retry_count,),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def test_dispatch_once_fails_poison_row_past_config_max_retries(self) -> None:
        instance = _instance()
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        try:
            self.assertEqual(runtime.config.max_retries, 3)  # default
            eid = self._poison_row(instance, retry_count=3)
            with mock.patch(
                "gateway.runtime.invoke_brain",
                side_effect=AssertionError("poison row must not reach the brain"),
            ):
                self.assertFalse(runtime.dispatch_once())
            conn = queue.connect(instance)
            try:
                row = conn.execute(
                    "SELECT status, retry_count, error FROM events WHERE id=?",
                    (eid,),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["retry_count"], 4)
            self.assertIn("max retries exceeded", row["error"])
        finally:
            runtime.close()

    def test_dispatch_once_still_retries_below_cap(self) -> None:
        instance = _instance()
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        try:
            eid = self._poison_row(instance, retry_count=0)
            with mock.patch(
                "gateway.runtime.invoke_brain",
                return_value=BrainResult("recovered", "sess-p"),
            ), mock.patch("gateway.runtime.deliver_response", return_value="m-out"):
                self.assertTrue(runtime.dispatch_once())
            conn = queue.connect(instance)
            try:
                row = conn.execute(
                    "SELECT status, retry_count FROM events WHERE id=?", (eid,)
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["retry_count"], 1)
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
