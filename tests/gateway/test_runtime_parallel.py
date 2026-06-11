"""Integration tests for parallel-slot dispatch in the gateway runtime.

Covers (deterministic routing — docs/specs/deterministic-slot-routing.md):
- two overlapping events on the same conversation run concurrently when N=2
  (asserted by overlap of the mocked brain's sleep windows);
- an explicit reply arriving while its original slot is busy is requeued
  (status flips back to `queued`) so it resumes on that slot when it frees;
- slot assignments are persisted to the message_slots map.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue  # noqa: E402
from gateway.brain import BrainResult  # noqa: E402
from gateway.config import render_default_config  # noqa: E402
from gateway.runtime import GatewayRuntime  # noqa: E402


def _config(max_concurrent: int) -> str:
    text = render_default_config(default_brain="claude:sonnet-4-6")
    return text.replace(
        "parallel:\n  max_concurrent: 1",
        f"parallel:\n  max_concurrent: {max_concurrent}",
    )


def _instance(max_concurrent: int) -> Path:
    root = Path(tempfile.mkdtemp(prefix="jc-runtime-parallel-"))
    (root / ".jc").write_text("", encoding="utf-8")
    (root / "ops").mkdir()
    (root / "memory" / "L1").mkdir(parents=True)
    (root / "memory" / "L1" / "IDENTITY.md").write_text("test", encoding="utf-8")
    (root / "ops" / "gateway.yaml").write_text(_config(max_concurrent), encoding="utf-8")
    return root


def _enqueue(
    instance: Path, *, content: str, message_id: str, reply_to: str | None = None
) -> int:
    meta: dict = {"chat_id": "28547271"}
    if reply_to is not None:
        meta["reply_to_message_id"] = reply_to
    conn = queue.connect(instance)
    try:
        event, _ = queue.enqueue(
            conn,
            source="telegram",
            source_message_id=message_id,
            conversation_id="28547271",
            content=content,
            meta=meta,
        )
    finally:
        conn.close()
    return event.id


def _wait_for_threads(runtime: GatewayRuntime, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with runtime._slot_busy_lock:
            active = [t for t in runtime._slot_active_threads if t.is_alive()]
        if not active:
            return
        time.sleep(0.05)
    raise AssertionError("slot threads did not finish within timeout")


class ParallelDispatchTests(unittest.TestCase):
    def test_two_overlapping_events_run_concurrently(self) -> None:
        instance = _instance(max_concurrent=2)
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        starts: list[float] = []
        ends: list[float] = []
        starts_lock = threading.Lock()

        def fake_invoke(*args, **kwargs):
            with starts_lock:
                starts.append(time.monotonic())
            time.sleep(0.4)
            with starts_lock:
                ends.append(time.monotonic())
            return BrainResult("ok", "sess-fake")

        try:
            _enqueue(instance, content="first message", message_id="m1")
            _enqueue(instance, content="second message", message_id="m2")
            with mock.patch("gateway.runtime.invoke_brain", side_effect=fake_invoke), \
                 mock.patch("gateway.runtime.deliver_response", return_value="m-out"):
                # Claim + dispatch each event individually. Parallel path
                # spawns a worker thread per event and returns immediately.
                # m1 → slot 0 (rule 3, cold conversation); m2 arrives while
                # slot 0 is busy → rule 3 → slot 1, no requeue.
                self.assertTrue(runtime.dispatch_once())
                self.assertTrue(runtime.dispatch_once())
                _wait_for_threads(runtime)

            # Both assignments persisted to the message→slot map.
            conn = queue.connect(instance)
            try:
                slots = {
                    queue.slot_for_message(conn, channel="telegram", message_id="m1"),
                    queue.slot_for_message(conn, channel="telegram", message_id="m2"),
                }
            finally:
                conn.close()
            self.assertEqual(slots, {0, 1})

            self.assertEqual(len(starts), 2)
            self.assertEqual(len(ends), 2)
            # If serial, second start > first end. Parallel requires overlap.
            second_start = sorted(starts)[1]
            first_end = sorted(ends)[0]
            self.assertLess(
                second_start,
                first_end,
                msg=(
                    "expected overlap between slot brain invocations: "
                    f"starts={starts} ends={ends}"
                ),
            )
        finally:
            runtime.close()

    def test_reply_to_busy_slot_is_requeued(self) -> None:
        instance = _instance(max_concurrent=2)
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        gate = threading.Event()
        invoked = threading.Event()

        def slow_invoke(*args, **kwargs):
            invoked.set()
            gate.wait(timeout=2.0)
            return BrainResult("ok", "sess-fake")

        try:
            id_first = _enqueue(instance, content="kick off slot 0", message_id="m1")
            with mock.patch("gateway.runtime.invoke_brain", side_effect=slow_invoke), \
                 mock.patch("gateway.runtime.deliver_response", return_value="m-out"):
                self.assertTrue(runtime.dispatch_once())
                # Wait until slot worker has started.
                invoked.wait(timeout=2.0)
                # Explicit reply to m1 while m1's slot (0) is busy → rule 1
                # queues it behind that slot; it must NOT start on slot 1.
                id_followup = _enqueue(
                    instance, content="follow-up", message_id="m2", reply_to="m1"
                )
                self.assertTrue(runtime.dispatch_once())

                conn = queue.connect(instance)
                try:
                    row = conn.execute(
                        "SELECT status, available_at FROM events WHERE id=?",
                        (id_followup,),
                    ).fetchone()
                finally:
                    conn.close()
                self.assertEqual(row["status"], "queued")
                # Released to be re-claimable after the busy slot frees.
                gate.set()
                _wait_for_threads(runtime)
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
