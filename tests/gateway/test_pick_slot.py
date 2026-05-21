"""Matrix tests for `GatewayRuntime._pick_slot` under parallel dispatch.

Each case fixes (busy slots, classifier verdict) and asserts the spec rule:

    related → busy   ⇒ (slot, queue=True)
    related → free   ⇒ (slot, queue=False)
    unrelated + free ⇒ (LRU free slot, queue=False)
    all busy + none  ⇒ (0, queue=True)   # main lane fallback

`max_concurrent <= 1` is also covered — `_pick_slot` must early-return
`(0, False)` without any classifier work.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue, sessions  # noqa: E402
from gateway.config import render_default_config  # noqa: E402
from gateway.runtime import GatewayRuntime  # noqa: E402


def _config(max_concurrent: int) -> str:
    text = render_default_config(default_brain="claude:sonnet-4-6")
    return text.replace(
        "parallel:\n  max_concurrent: 1",
        f"parallel:\n  max_concurrent: {max_concurrent}",
    )


def _instance(max_concurrent: int) -> Path:
    root = Path(tempfile.mkdtemp(prefix="jc-pick-slot-"))
    (root / ".jc").write_text("", encoding="utf-8")
    (root / "ops").mkdir()
    (root / "ops" / "gateway.yaml").write_text(_config(max_concurrent), encoding="utf-8")
    return root


def _enqueue(instance: Path, *, content: str = "hi", conversation_id: str = "c1") -> queue.Event:
    conn = queue.connect(instance)
    try:
        event, _ = queue.enqueue(
            conn,
            source="telegram",
            source_message_id=f"m-{content[:5]}",
            conversation_id=conversation_id,
            content=content,
            meta={"chat_id": conversation_id},
        )
    finally:
        conn.close()
    return event


def _build_runtime(instance: Path) -> GatewayRuntime:
    return GatewayRuntime(
        instance,
        log_path=queue.queue_dir(instance) / "test.log",
        stop_requested=lambda: True,
    )


class PickSlotMatrixTests(unittest.TestCase):
    def test_n1_returns_slot_zero_without_classifier(self) -> None:
        instance = _instance(max_concurrent=1)
        runtime = _build_runtime(instance)
        try:
            event = _enqueue(instance)
            with mock.patch.object(
                runtime, "_classify_slot_affinity", side_effect=AssertionError(
                    "classifier must NOT fire for N=1"
                )
            ):
                self.assertEqual(runtime._pick_slot(event), (0, False))
        finally:
            runtime.close()

    def test_no_conversation_id_returns_slot_zero(self) -> None:
        instance = _instance(max_concurrent=3)
        runtime = _build_runtime(instance)
        try:
            event = queue.Event(
                id=1, source="cron", source_message_id=None, user_id=None,
                conversation_id=None, content="hi", meta=None, status="running",
                received_at="2026-01-01T00:00:00Z",
                available_at="2026-01-01T00:00:00Z",
                locked_by="w", locked_until=None, started_at=None,
                finished_at=None, retry_count=0, response=None, error=None,
            )
            self.assertEqual(runtime._pick_slot(event), (0, False))
        finally:
            runtime.close()

    def test_related_to_free_slot_resumes(self) -> None:
        instance = _instance(max_concurrent=3)
        runtime = _build_runtime(instance)
        try:
            event = _enqueue(instance, content="follow-up to slot 1")
            with mock.patch.object(runtime, "_classify_slot_affinity", return_value=1), \
                 mock.patch.object(runtime, "_slot_summaries", return_value={0: "x", 1: "y"}):
                self.assertEqual(runtime._pick_slot(event), (1, False))
        finally:
            runtime.close()

    def test_related_to_busy_slot_queues(self) -> None:
        instance = _instance(max_concurrent=3)
        runtime = _build_runtime(instance)
        try:
            event = _enqueue(instance, content="follow-up to slot 1")
            runtime._busy_slots[("telegram", "c1")] = {1}
            with mock.patch.object(runtime, "_classify_slot_affinity", return_value=1), \
                 mock.patch.object(runtime, "_slot_summaries", return_value={0: "x", 1: "y"}):
                slot, queue_it = runtime._pick_slot(event)
                self.assertEqual(slot, 1)
                self.assertTrue(queue_it)
        finally:
            runtime.close()

    def test_unrelated_picks_lowest_free_slot_when_no_history(self) -> None:
        instance = _instance(max_concurrent=3)
        runtime = _build_runtime(instance)
        try:
            event = _enqueue(instance, content="completely new topic")
            # Slot 0 busy; slots 1 and 2 free. No session rows → tie-break = lowest id.
            runtime._busy_slots[("telegram", "c1")] = {0}
            with mock.patch.object(runtime, "_classify_slot_affinity", return_value=None), \
                 mock.patch.object(runtime, "_slot_summaries", return_value={0: "x"}):
                slot, queue_it = runtime._pick_slot(event)
                self.assertEqual(slot, 1)
                self.assertFalse(queue_it)
        finally:
            runtime.close()

    def test_unrelated_picks_lru_free_slot_using_session_timestamps(self) -> None:
        instance = _instance(max_concurrent=3)
        runtime = _build_runtime(instance)
        try:
            conn = queue.connect(instance)
            try:
                # Slot 1 used most recently; slot 2 stamped earlier.
                # LRU = slot 2 → must be picked.
                sessions.upsert_session(
                    conn, channel="telegram", conversation_id="c1",
                    brain="claude", session_id="s2", slot=2,
                )
                sessions.upsert_session(
                    conn, channel="telegram", conversation_id="c1",
                    brain="claude", session_id="s1", slot=1,
                )
                # Patch updated_at directly: lower ts = LRU.
                conn.execute(
                    "UPDATE sessions SET updated_at='2026-05-01T00:00:00Z' WHERE slot=2"
                )
                conn.execute(
                    "UPDATE sessions SET updated_at='2026-05-21T00:00:00Z' WHERE slot=1"
                )
                conn.commit()
            finally:
                conn.close()
            event = _enqueue(instance, content="new")
            # Slot 0 busy; among 1+2 free, LRU = 2.
            runtime._busy_slots[("telegram", "c1")] = {0}
            with mock.patch.object(runtime, "_classify_slot_affinity", return_value=None), \
                 mock.patch.object(runtime, "_slot_summaries", return_value={0: "x"}):
                slot, queue_it = runtime._pick_slot(event)
                self.assertEqual(slot, 2)
                self.assertFalse(queue_it)
        finally:
            runtime.close()

    def test_all_busy_unrelated_queues_main_lane(self) -> None:
        instance = _instance(max_concurrent=2)
        runtime = _build_runtime(instance)
        try:
            event = _enqueue(instance, content="new but all busy")
            runtime._busy_slots[("telegram", "c1")] = {0, 1}
            with mock.patch.object(runtime, "_classify_slot_affinity", return_value=None), \
                 mock.patch.object(runtime, "_slot_summaries", return_value={0: "x", 1: "y"}):
                slot, queue_it = runtime._pick_slot(event)
                self.assertEqual(slot, 0)
                self.assertTrue(queue_it)
        finally:
            runtime.close()

    def test_classifier_returning_out_of_range_slot_falls_back_to_lru(self) -> None:
        instance = _instance(max_concurrent=2)
        runtime = _build_runtime(instance)
        try:
            event = _enqueue(instance)
            # Classifier says slot 7 — out of range, must be ignored.
            with mock.patch.object(runtime, "_classify_slot_affinity", return_value=7), \
                 mock.patch.object(runtime, "_slot_summaries", return_value={0: "x"}):
                slot, queue_it = runtime._pick_slot(event)
                self.assertEqual(slot, 0)
                self.assertFalse(queue_it)
        finally:
            runtime.close()

    def test_classifier_exception_does_not_kill_dispatch(self) -> None:
        instance = _instance(max_concurrent=2)
        runtime = _build_runtime(instance)
        try:
            event = _enqueue(instance)
            with mock.patch.object(
                runtime, "_classify_slot_affinity", side_effect=RuntimeError("boom")
            ), mock.patch.object(runtime, "_slot_summaries", return_value={0: "x"}):
                slot, queue_it = runtime._pick_slot(event)
                self.assertEqual(slot, 0)
                self.assertFalse(queue_it)
        finally:
            runtime.close()


class ParseSlotVerdictTests(unittest.TestCase):
    def test_unrelated_returns_none(self) -> None:
        from gateway.runtime import _parse_slot_verdict
        self.assertIsNone(_parse_slot_verdict("unrelated"))
        self.assertIsNone(_parse_slot_verdict(" UNRELATED \n"))
        self.assertIsNone(_parse_slot_verdict("`unrelated`"))

    def test_related_returns_int(self) -> None:
        from gateway.runtime import _parse_slot_verdict
        self.assertEqual(_parse_slot_verdict("related:2"), 2)
        self.assertEqual(_parse_slot_verdict("RELATED:0\n"), 0)
        self.assertEqual(_parse_slot_verdict("`related:1`"), 1)

    def test_malformed_returns_none(self) -> None:
        from gateway.runtime import _parse_slot_verdict
        self.assertIsNone(_parse_slot_verdict(""))
        self.assertIsNone(_parse_slot_verdict("related:abc"))
        self.assertIsNone(_parse_slot_verdict("idk"))


if __name__ == "__main__":
    unittest.main()
