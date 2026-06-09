"""Per-claim lease tokens + pre-delivery ownership gate (audit Finding 1/2).

The per-process worker id (`gateway-<pid>`) defeats every
`status='running' AND locked_by=?` guard when the SAME process re-claims an
event after lease loss: the stale thread and the fresh thread share the id,
both pass complete/fail/renew, and both deliver → duplicate replies. Claims
now mint a fresh `<worker_id>#<12-hex>` token per claim; delivery is gated
on still owning every row of the claim.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue  # noqa: E402


def _instance() -> Path:
    root = Path(tempfile.mkdtemp(prefix="jc-claimtoken-"))
    (root / ".jc").write_text("", encoding="utf-8")
    return root


class ClaimTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _instance()

    def _enqueue(self, conn, msg_id: str = "m1") -> int:
        event, _ = queue.enqueue(
            conn,
            source="telegram",
            source_message_id=msg_id,
            conversation_id="c1",
            content="hello",
            user_id="u1",
        )
        return event.id

    def test_claim_mints_token_with_worker_prefix(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            self._enqueue(conn)
            event = queue.claim_next(conn, worker_id="gateway-123")
            self.assertIsNotNone(event)
            self.assertTrue(event.locked_by.startswith("gateway-123#"))
            self.assertTrue(queue.is_claim_token(event.locked_by))
        finally:
            conn.close()

    def test_reclaim_after_expiry_gets_different_token(self) -> None:
        # The duplicate-reply scenario: same worker_id (same process)
        # re-claims after lease expiry. The stale token must no longer
        # match the row.
        conn = queue.connect(self.tmp)
        try:
            eid = self._enqueue(conn)
            stale = queue.claim_next(conn, worker_id="gateway-123", lease_seconds=300)
            self.assertEqual(stale.id, eid)
            # Simulate lease expiry + requeue.
            conn.execute(
                "UPDATE events SET locked_until='2000-01-01T00:00:00Z' WHERE id=?",
                (eid,),
            )
            conn.commit()
            queue.requeue_expired(conn)
            conn.commit()
            fresh = queue.claim_next(conn, worker_id="gateway-123", lease_seconds=300)
            self.assertEqual(fresh.id, eid)
            self.assertNotEqual(stale.locked_by, fresh.locked_by)

            # Stale token fails every guard even though worker_id matches.
            with self.assertRaises(KeyError):
                queue.complete(
                    conn, eid, response="dup", expected_locked_by=stale.locked_by
                )
            self.assertEqual(
                queue.renew_lease(conn, [eid], worker_id=stale.locked_by), 0
            )
            self.assertEqual(
                queue.owned_count(conn, [eid], locked_by=stale.locked_by), 0
            )
            # Fresh token passes.
            self.assertEqual(
                queue.owned_count(conn, [eid], locked_by=fresh.locked_by), 1
            )
            done = queue.complete(
                conn, eid, response="ok", expected_locked_by=fresh.locked_by
            )
            self.assertEqual(done.status, "done")
        finally:
            conn.close()

    def test_owned_count_partial_batch(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            self._enqueue(conn, "m1")
            self._enqueue(conn, "m2")
            batch = queue.claim_batch_same_conversation(conn, worker_id="w")
            self.assertEqual(len(batch), 2)
            token = batch[0].locked_by
            ids = [e.id for e in batch]
            self.assertEqual(queue.owned_count(conn, ids, locked_by=token), 2)
            # One row gets re-claimed elsewhere → partial ownership.
            conn.execute(
                "UPDATE events SET locked_by='other#abc' WHERE id=?", (ids[1],)
            )
            conn.commit()
            self.assertEqual(queue.owned_count(conn, ids, locked_by=token), 1)
        finally:
            conn.close()

    def test_owned_count_empty_ids(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            self.assertEqual(queue.owned_count(conn, [], locked_by="x#y"), 0)
        finally:
            conn.close()

    def test_is_claim_token(self) -> None:
        self.assertTrue(queue.is_claim_token("gateway-1#abc123"))
        self.assertFalse(queue.is_claim_token("gateway-1"))
        self.assertFalse(queue.is_claim_token(""))
        self.assertFalse(queue.is_claim_token(None))


class DeliveryOwnershipGateTests(unittest.TestCase):
    """Runtime-level gate: deliver only while every claimed row is still ours."""

    def setUp(self) -> None:
        from gateway.config import render_default_config  # noqa: WPS433

        self.tmp = _instance()
        (self.tmp / "ops").mkdir()
        (self.tmp / "ops" / "gateway.yaml").write_text(
            render_default_config(default_brain="claude:sonnet-4-6"),
            encoding="utf-8",
        )
        from gateway.runtime import GatewayRuntime  # noqa: WPS433

        self.runtime = GatewayRuntime(
            self.tmp,
            log_path=queue.queue_dir(self.tmp) / "test.log",
            stop_requested=lambda: True,
        )

    def tearDown(self) -> None:
        self.runtime.close()

    def _claimed_event(self):
        conn = queue.connect(self.tmp)
        try:
            queue.enqueue(
                conn,
                source="telegram",
                source_message_id="m1",
                conversation_id="c1",
                content="hi",
                user_id="u1",
            )
            return queue.claim_next(conn, worker_id=self.runtime.worker_id)
        finally:
            conn.close()

    def test_owned_event_passes_gate(self) -> None:
        event = self._claimed_event()
        self.assertTrue(self.runtime._delivery_ownership_ok(event))

    def test_reclaimed_event_fails_gate(self) -> None:
        event = self._claimed_event()
        conn = queue.connect(self.tmp)
        try:
            conn.execute(
                "UPDATE events SET locked_by='gateway-999#fresh0token' WHERE id=?",
                (event.id,),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertFalse(self.runtime._delivery_ownership_ok(event))

    def test_unclaimed_event_not_gated(self) -> None:
        import dataclasses

        event = self._claimed_event()
        legacy = dataclasses.replace(event, locked_by=None)
        self.assertTrue(self.runtime._delivery_ownership_ok(legacy))
        non_token = dataclasses.replace(event, locked_by="w1")
        self.assertTrue(self.runtime._delivery_ownership_ok(non_token))


if __name__ == "__main__":
    unittest.main()
