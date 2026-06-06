"""§18 conversation-scoped compaction — rotation, busy-slot queueing, notify."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue, sessions  # noqa: E402
from gateway.lifecycle import compaction, telemetry  # noqa: E402


class _Runtime:
    def __init__(self, instance: Path):
        self.instance_dir = instance
        self.config = object()
        self.logs: list[str] = []

    def log(self, msg, *, kind="", **_):
        self.logs.append(msg)


def _instance() -> Path:
    root = Path(tempfile.mkdtemp(prefix="jc-compaction-"))
    conn = queue.connect(root)
    try:
        for brain, slot in (("claude", 0), ("codex", 1)):
            sessions.upsert_session(
                conn,
                channel="telegram",
                conversation_id="conv1",
                brain=brain,
                session_id=f"sess-{brain}-{slot}",
                slot=slot,
            )
            telemetry.record_usage(
                conn,
                owner_key=compaction.owner_key("telegram", "conv1", brain, slot),
                brain=brain,
                usage=telemetry.ContextUsage.from_anthropic_usage({"input_tokens": 120_000}),
            )
    finally:
        conn.close()
    return root


class RotateSlotTest(unittest.TestCase):
    def test_rotation_clears_session_and_records(self) -> None:
        inst = _instance()
        conn = queue.connect(inst)
        try:
            result = compaction.rotate_slot(
                conn,
                channel="telegram",
                conversation_id="conv1",
                brain="claude",
                slot=0,
                expected_session_id="sess-claude-0",
            )
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.tokens_before, 120_000)
            self.assertEqual(result.tokens_after, 0)
            remaining = compaction.list_conversation_slots(
                conn, channel="telegram", conversation_id="conv1"
            )
            self.assertEqual([s.slot for s in remaining], [1])
        finally:
            conn.close()

    def test_stale_expected_session_is_noop(self) -> None:
        inst = _instance()
        conn = queue.connect(inst)
        try:
            result = compaction.rotate_slot(
                conn,
                channel="telegram",
                conversation_id="conv1",
                brain="claude",
                slot=0,
                expected_session_id="WRONG",
            )
            self.assertIsNone(result)
        finally:
            conn.close()


class CompactConversationTest(unittest.TestCase):
    def test_busy_slot_queued_other_rotated_and_notified(self) -> None:
        inst = _instance()
        rt = _Runtime(inst)
        with mock.patch(
            "gateway.lifecycle.compaction.notify.notify_compaction", return_value=True
        ) as notify_fn:
            result = compaction.compact_conversation(
                rt,
                channel="telegram",
                conversation_id="conv1",
                trigger="/compact",
                busy_slots={1},
            )
        self.assertEqual([s.brain for s in result.compacted], ["claude"])
        self.assertEqual([s.slot for s in result.queued], [1])
        notify_fn.assert_called_once()
        self.assertIn("rotated", result.report)
        self.assertIn("queued 1", result.report)
        self.assertTrue(
            any("context_compaction_deferred" in line for line in rt.logs),
            rt.logs,
        )

    def test_no_slots_no_notify(self) -> None:
        inst = _instance()
        # rotate everything first
        conn = queue.connect(inst)
        try:
            for brain, slot in (("claude", 0), ("codex", 1)):
                compaction.rotate_slot(
                    conn,
                    channel="telegram",
                    conversation_id="conv1",
                    brain=brain,
                    slot=slot,
                )
        finally:
            conn.close()
        rt = _Runtime(inst)
        with mock.patch(
            "gateway.lifecycle.compaction.notify.notify_compaction", return_value=True
        ) as notify_fn:
            result = compaction.compact_conversation(
                rt, channel="telegram", conversation_id="conv1", trigger="/compact"
            )
        self.assertEqual(result.compacted, [])
        notify_fn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
