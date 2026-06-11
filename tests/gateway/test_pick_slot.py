"""Matrix tests for `GatewayRuntime._pick_slot` under parallel dispatch.

Deterministic rules (docs/specs/deterministic-slot-routing.md) — no
classifier, no mocking of routing internals:

    1. reply & slot(reply_to) known → reuse that slot (queue behind if busy)
    2. conversation's current slot free → resume it (sequential continuity)
    3. free slot exists → next progressive free slot (lowest free id)
    4. all busy → queue on slot 0 (main lane)

`max_concurrent <= 1` is also covered — `_pick_slot` must early-return
`(0, False)` without touching the routing state.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue  # noqa: E402
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


def _enqueue(
    instance: Path,
    *,
    content: str = "hi",
    conversation_id: str = "c1",
    message_id: str | None = None,
    reply_to: str | None = None,
) -> queue.Event:
    meta: dict = {"chat_id": conversation_id}
    if reply_to is not None:
        meta["reply_to_message_id"] = reply_to
    conn = queue.connect(instance)
    try:
        event, _ = queue.enqueue(
            conn,
            source="telegram",
            source_message_id=message_id or f"m-{content[:8]}",
            conversation_id=conversation_id,
            content=content,
            meta=meta,
        )
    finally:
        conn.close()
    return event


def _record(
    instance: Path,
    *,
    message_id: str,
    slot: int,
    conversation_id: str = "c1",
    channel: str = "telegram",
) -> None:
    conn = queue.connect(instance)
    try:
        queue.record_message_slot(
            conn,
            channel=channel,
            conversation_id=conversation_id,
            message_id=message_id,
            slot=slot,
        )
    finally:
        conn.close()


def _build_runtime(instance: Path) -> GatewayRuntime:
    return GatewayRuntime(
        instance,
        log_path=queue.queue_dir(instance) / "test.log",
        stop_requested=lambda: True,
    )


class PickSlotSerialTests(unittest.TestCase):
    def test_n1_returns_slot_zero(self) -> None:
        instance = _instance(max_concurrent=1)
        runtime = _build_runtime(instance)
        try:
            event = _enqueue(instance)
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


class PickSlotReplyTests(unittest.TestCase):
    """Rule 1 — explicit reply forces the original message's slot."""

    def test_reply_reuses_recorded_slot_when_free(self) -> None:
        instance = _instance(max_concurrent=3)
        runtime = _build_runtime(instance)
        try:
            _record(instance, message_id="m-orig", slot=1)
            event = _enqueue(instance, content="reply", reply_to="m-orig")
            self.assertEqual(runtime._pick_slot(event), (1, False))
        finally:
            runtime.close()

    def test_reply_to_busy_slot_queues_behind_it(self) -> None:
        instance = _instance(max_concurrent=3)
        runtime = _build_runtime(instance)
        try:
            _record(instance, message_id="m-orig", slot=1)
            runtime._busy_slots[("telegram", "c1")] = {1}
            event = _enqueue(instance, content="reply", reply_to="m-orig")
            # Never start a reply on a different slot — queue behind slot 1
            # even though slots 0 and 2 are free.
            self.assertEqual(runtime._pick_slot(event), (1, True))
        finally:
            runtime.close()

    def test_reply_to_unknown_message_falls_through(self) -> None:
        instance = _instance(max_concurrent=3)
        runtime = _build_runtime(instance)
        try:
            event = _enqueue(instance, content="reply", reply_to="m-never-seen")
            # No record for the original → rules 2-4. Cold conversation,
            # all free → progressive slot 0.
            self.assertEqual(runtime._pick_slot(event), (0, False))
        finally:
            runtime.close()

    def test_reply_slot_out_of_range_falls_through(self) -> None:
        instance = _instance(max_concurrent=2)
        runtime = _build_runtime(instance)
        try:
            # Recorded under a larger max_concurrent than today's config.
            # Out of range for both rule 1 (reply) and rule 2 (continuity)
            # → rule 3 picks the lowest free id.
            _record(instance, message_id="m-orig", slot=7)
            event = _enqueue(instance, content="reply", reply_to="m-orig")
            self.assertEqual(runtime._pick_slot(event), (0, False))
        finally:
            runtime.close()


class PickSlotContinuityTests(unittest.TestCase):
    """Rule 2 — non-reply resumes the conversation's current slot if free."""

    def test_non_reply_resumes_current_slot_when_free(self) -> None:
        instance = _instance(max_concurrent=3)
        runtime = _build_runtime(instance)
        try:
            _record(instance, message_id="m-prev", slot=2)
            event = _enqueue(instance, content="next message")
            # Slot 2 free → resume it; NOT a fresh slot per message.
            self.assertEqual(runtime._pick_slot(event), (2, False))
        finally:
            runtime.close()

    def test_non_reply_current_slot_busy_picks_next_progressive_free(self) -> None:
        instance = _instance(max_concurrent=3)
        runtime = _build_runtime(instance)
        try:
            _record(instance, message_id="m-prev", slot=0)
            runtime._busy_slots[("telegram", "c1")] = {0}
            event = _enqueue(instance, content="overlapping message")
            # Genuine overlap → new parallel lane, lowest free id (1).
            self.assertEqual(runtime._pick_slot(event), (1, False))
        finally:
            runtime.close()

    def test_continuity_tracks_most_recent_assignment(self) -> None:
        instance = _instance(max_concurrent=4)
        runtime = _build_runtime(instance)
        try:
            _record(instance, message_id="m-1", slot=0)
            _record(instance, message_id="m-2", slot=3)
            event = _enqueue(instance, content="follow-up")
            # Most recent assignment (slot 3) wins, not the oldest.
            self.assertEqual(runtime._pick_slot(event), (3, False))
        finally:
            runtime.close()

    def test_conversations_do_not_share_continuity(self) -> None:
        instance = _instance(max_concurrent=3)
        runtime = _build_runtime(instance)
        try:
            _record(instance, message_id="m-other", slot=2, conversation_id="c-other")
            event = _enqueue(instance, content="hello", conversation_id="c1")
            # c1 is cold — c-other's slot must not leak in. Progressive → 0.
            self.assertEqual(runtime._pick_slot(event), (0, False))
        finally:
            runtime.close()


class PickSlotProgressiveTests(unittest.TestCase):
    """Rules 3 + 4 — progressive free slot, then main-lane queue."""

    def test_cold_conversation_picks_slot_zero(self) -> None:
        instance = _instance(max_concurrent=3)
        runtime = _build_runtime(instance)
        try:
            event = _enqueue(instance, content="first ever")
            self.assertEqual(runtime._pick_slot(event), (0, False))
        finally:
            runtime.close()

    def test_progressive_is_lowest_free_id_not_lru(self) -> None:
        instance = _instance(max_concurrent=4)
        runtime = _build_runtime(instance)
        try:
            _record(instance, message_id="m-prev", slot=1)
            runtime._busy_slots[("telegram", "c1")] = {0, 1}
            event = _enqueue(instance, content="burst message")
            # Free slots: 2, 3. Progressive = 2 (lowest id), deterministic.
            self.assertEqual(runtime._pick_slot(event), (2, False))
        finally:
            runtime.close()

    def test_all_busy_queues_on_main_lane(self) -> None:
        instance = _instance(max_concurrent=2)
        runtime = _build_runtime(instance)
        try:
            _record(instance, message_id="m-prev", slot=0)
            runtime._busy_slots[("telegram", "c1")] = {0, 1}
            event = _enqueue(instance, content="new but all busy")
            self.assertEqual(runtime._pick_slot(event), (0, True))
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
