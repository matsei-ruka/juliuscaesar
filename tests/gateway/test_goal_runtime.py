"""Runtime set/clear goal hooks (PR #65), tested at the dispatch-loop seam."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import goal_cache, queue  # noqa: E402
from gateway.runtime import GatewayRuntime  # noqa: E402


def _fake_runtime(inst: Path):
    return SimpleNamespace(instance_dir=inst, worker_id="w", log=lambda *a, **k: None)


def _enqueue_and_claim(inst: Path, *, meta: dict, conversation_id: str):
    conn = queue.connect(inst)
    try:
        event, _ = queue.enqueue(
            conn,
            source="company-inbox",
            content="Title T\n\nbody",
            source_message_id=meta.get("source_message_id", "m1"),
            conversation_id=conversation_id,
            meta=meta,
        )
        claimed = queue.claim_next(conn, worker_id="w")
    finally:
        conn.close()
    return claimed


def _status_counts(inst: Path) -> dict:
    conn = queue.connect(inst)
    try:
        return queue.counts(conn)
    finally:
        conn.close()


class GoalLifecycleHookTests(unittest.TestCase):
    def test_task_assigned_sets_goal_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            event = _enqueue_and_claim(
                inst,
                meta={"kind": "task_assigned", "task_id": "t1", "title": "Ship it", "description": "now"},
                conversation_id="task-root:R",
            )
            handled = GatewayRuntime._apply_goal_lifecycle(_fake_runtime(inst), event, [event.id])
            self.assertFalse(handled)  # must continue to brain dispatch
            self.assertEqual(goal_cache.goal_text(inst, "task-root:R"), "Ship it\n\nnow")
            # event still running (not completed by the hook)
            self.assertEqual(_status_counts(inst).get("running", 0), 1)

    def test_task_closed_clears_goal_and_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            goal_cache.set(inst, "task-root:R", "t1", "Ship it")
            event = _enqueue_and_claim(
                inst,
                meta={"kind": "task_closed", "task_id": "t1", "source_message_id": "task-closed:t1"},
                conversation_id="task-root:R",
            )
            handled = GatewayRuntime._apply_goal_lifecycle(_fake_runtime(inst), event, [event.id])
            self.assertTrue(handled)  # control event — no brain dispatch
            self.assertIsNone(goal_cache.get(inst, "task-root:R"))
            counts = _status_counts(inst)
            self.assertEqual(counts.get("done", 0), 1)
            self.assertEqual(counts.get("running", 0), 0)

    def test_task_closed_stale_task_id_preserves_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            goal_cache.set(inst, "task-root:R", "NEW", "current goal")
            event = _enqueue_and_claim(
                inst,
                meta={"kind": "task_closed", "task_id": "OLD", "source_message_id": "task-closed:OLD"},
                conversation_id="task-root:R",
            )
            handled = GatewayRuntime._apply_goal_lifecycle(_fake_runtime(inst), event, [event.id])
            self.assertTrue(handled)  # still a control event (completed)
            # stale close for OLD must not drop the active NEW goal
            self.assertEqual(goal_cache.goal_text(inst, "task-root:R"), "current goal")

    def test_non_task_event_is_passthrough(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            event = _enqueue_and_claim(
                inst, meta={"kind": "other"}, conversation_id="telegram:1"
            )
            handled = GatewayRuntime._apply_goal_lifecycle(_fake_runtime(inst), event, [event.id])
            self.assertFalse(handled)
            self.assertEqual(goal_cache.all_goals(inst), {})


if __name__ == "__main__":
    unittest.main()
