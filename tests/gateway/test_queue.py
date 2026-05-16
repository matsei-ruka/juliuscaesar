"""Unit tests for gateway.queue helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gateway import queue


class ClaimBatchSameConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jc-queue-test-"))
        (self.tmp / ".jc").write_text("", encoding="utf-8")

    def test_batches_same_conversation_only(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            a, _ = queue.enqueue(
                conn,
                source="telegram",
                source_message_id="m1",
                conversation_id="group-1",
                content="alice line",
                user_id="u-alice",
            )
            b, _ = queue.enqueue(
                conn,
                source="telegram",
                source_message_id="m2",
                conversation_id="group-1",
                content="bob line",
                user_id="u-bob",
            )
            c, _ = queue.enqueue(
                conn,
                source="telegram",
                source_message_id="m3",
                conversation_id="group-1",
                content="carol line",
                user_id="u-carol",
            )
            other, _ = queue.enqueue(
                conn,
                source="telegram",
                source_message_id="m4",
                conversation_id="group-2",
                content="unrelated",
                user_id="u-zoe",
            )

            batch = queue.claim_batch_same_conversation(
                conn,
                worker_id="worker-test",
            )

            ids = [e.id for e in batch]
            self.assertEqual(ids, [a.id, b.id, c.id])
            for event in batch:
                self.assertEqual(event.status, "running")
                self.assertEqual(event.conversation_id, "group-1")
                self.assertEqual(event.locked_by, "worker-test")

            row = conn.execute(
                "SELECT status FROM events WHERE id=?", (other.id,)
            ).fetchone()
            self.assertEqual(row["status"], "queued")

            second_batch = queue.claim_batch_same_conversation(
                conn,
                worker_id="worker-test",
            )
            self.assertEqual([e.id for e in second_batch], [other.id])
        finally:
            conn.close()

    def test_empty_when_nothing_queued(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            self.assertEqual(
                queue.claim_batch_same_conversation(conn, worker_id="w"),
                [],
            )
        finally:
            conn.close()

    def test_null_conversation_id_returns_single(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            a, _ = queue.enqueue(
                conn,
                source="cron",
                source_message_id="c1",
                conversation_id=None,
                content="first",
            )
            b, _ = queue.enqueue(
                conn,
                source="cron",
                source_message_id="c2",
                conversation_id=None,
                content="second",
            )

            batch = queue.claim_batch_same_conversation(conn, worker_id="w")
            self.assertEqual([e.id for e in batch], [a.id])

            row = conn.execute(
                "SELECT status FROM events WHERE id=?", (b.id,)
            ).fetchone()
            self.assertEqual(row["status"], "queued")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
