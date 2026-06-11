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
            # One fresh per-claim token, shared by the whole batch.
            token = batch[0].locked_by
            self.assertTrue(token.startswith("worker-test#"))
            self.assertTrue(queue.is_claim_token(token))
            for event in batch:
                self.assertEqual(event.status, "running")
                self.assertEqual(event.conversation_id, "group-1")
                self.assertEqual(event.locked_by, token)

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


class ResetRunningToQueuedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jc-reset-test-"))
        (self.tmp / ".jc").write_text("", encoding="utf-8")

    def _running_event(self, conn, *, meta: dict | None = None) -> int:
        import json as _json

        meta_text = _json.dumps(meta or {"chat_id": "1"})
        cur = conn.execute(
            """
            INSERT INTO events
              (source, content, meta, status, received_at, available_at, started_at,
               locked_by, locked_until)
            VALUES ('telegram', 'x', ?, 'running', '2026-05-17T10:00:00+00:00',
                    '2026-05-17T10:00:00+00:00', '2026-05-17T10:00:00+00:00',
                    'worker-1', '2026-05-17T10:05:00+00:00')
            """,
            (meta_text,),
        )
        conn.commit()
        return cur.lastrowid

    def test_resets_status_to_queued(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._running_event(conn)
            ok = queue.reset_running_to_queued(conn, eid)
            self.assertTrue(ok)
            row = conn.execute(
                "SELECT status, locked_by, locked_until, started_at FROM events WHERE id=?",
                (eid,),
            ).fetchone()
            self.assertEqual(row["status"], "queued")
            self.assertIsNone(row["locked_by"])
            self.assertIsNone(row["locked_until"])
            self.assertIsNone(row["started_at"])
        finally:
            conn.close()

    def test_refuses_to_reset_done_event(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._running_event(conn)
            conn.execute("UPDATE events SET status='done' WHERE id=?", (eid,))
            conn.commit()
            ok = queue.reset_running_to_queued(conn, eid)
            self.assertFalse(ok)
            row = conn.execute("SELECT status FROM events WHERE id=?", (eid,)).fetchone()
            self.assertEqual(row["status"], "done")
        finally:
            conn.close()

    def test_drops_resume_session_when_requested(self) -> None:
        import json as _json

        conn = queue.connect(self.tmp)
        try:
            eid = self._running_event(
                conn, meta={"chat_id": "1", "resume_session": "abc-uuid"}
            )
            ok = queue.reset_running_to_queued(conn, eid, drop_resume_session=True)
            self.assertTrue(ok)
            row = conn.execute("SELECT meta FROM events WHERE id=?", (eid,)).fetchone()
            meta = _json.loads(row["meta"])
            self.assertNotIn("resume_session", meta)
            self.assertEqual(meta.get("chat_id"), "1")
        finally:
            conn.close()

    def test_available_in_seconds_backoff(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._running_event(conn)
            queue.reset_running_to_queued(conn, eid, available_in_seconds=30)
            row = conn.execute(
                "SELECT available_at FROM events WHERE id=?", (eid,)
            ).fetchone()
            from datetime import datetime
            t = datetime.fromisoformat(row["available_at"].replace("Z", "+00:00"))
            self.assertIsNotNone(t)
        finally:
            conn.close()

    def test_missing_event_raises_keyerror(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            with self.assertRaises(KeyError):
                queue.reset_running_to_queued(conn, 99999)
        finally:
            conn.close()


class MarkEventFailedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jc-failed-test-"))
        (self.tmp / ".jc").write_text("", encoding="utf-8")

    def _running_event(self, conn) -> int:
        cur = conn.execute(
            """
            INSERT INTO events
              (source, content, meta, status, received_at, available_at, started_at,
               locked_by, locked_until)
            VALUES ('telegram', 'x', '{}', 'running', '2026-05-17T10:00:00+00:00',
                    '2026-05-17T10:00:00+00:00', '2026-05-17T10:00:00+00:00',
                    'worker-1', '2026-05-17T10:05:00+00:00')
            """
        )
        conn.commit()
        return cur.lastrowid

    def test_transitions_running_to_failed(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._running_event(conn)
            ok = queue.mark_event_failed(conn, eid)
            self.assertTrue(ok)
            row = conn.execute(
                "SELECT status, error, locked_by, locked_until FROM events WHERE id=?",
                (eid,),
            ).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["error"], "recovery_escalated")
            self.assertIsNone(row["locked_by"])
            self.assertIsNone(row["locked_until"])
        finally:
            conn.close()

    def test_refuses_non_running_event(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._running_event(conn)
            conn.execute("UPDATE events SET status='done' WHERE id=?", (eid,))
            conn.commit()
            ok = queue.mark_event_failed(conn, eid)
            self.assertFalse(ok)
            row = conn.execute("SELECT status FROM events WHERE id=?", (eid,)).fetchone()
            self.assertEqual(row["status"], "done")
        finally:
            conn.close()

    def test_missing_event_raises_keyerror(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            with self.assertRaises(KeyError):
                queue.mark_event_failed(conn, 99999)
        finally:
            conn.close()

    def test_custom_error_message(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._running_event(conn)
            queue.mark_event_failed(conn, eid, error="custom_reason")
            row = conn.execute("SELECT error FROM events WHERE id=?", (eid,)).fetchone()
            self.assertEqual(row["error"], "custom_reason")
        finally:
            conn.close()


class ResetRunningCASTests(unittest.TestCase):
    """Bug #2 — reset_running_to_queued must refuse to clobber a row whose
    locked_by has changed since the supervisor snapshot."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jc-reset-cas-test-"))
        (self.tmp / ".jc").write_text("", encoding="utf-8")

    def _running_event(self, conn, *, locked_by: str = "worker-1") -> int:
        cur = conn.execute(
            """
            INSERT INTO events
              (source, content, meta, status, received_at, available_at, started_at,
               locked_by, locked_until)
            VALUES ('telegram', 'x', '{}', 'running', '2026-05-17T10:00:00+00:00',
                    '2026-05-17T10:00:00+00:00', '2026-05-17T10:00:00+00:00',
                    ?, '2026-05-17T10:05:00+00:00')
            """,
            (locked_by,),
        )
        conn.commit()
        return cur.lastrowid

    def test_resets_when_locked_by_matches(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._running_event(conn, locked_by="worker-A")
            ok = queue.reset_running_to_queued(
                conn, eid, expected_locked_by="worker-A"
            )
            self.assertTrue(ok)
            row = conn.execute(
                "SELECT status FROM events WHERE id=?", (eid,)
            ).fetchone()
            self.assertEqual(row["status"], "queued")
        finally:
            conn.close()

    def test_refuses_when_locked_by_changed(self) -> None:
        """A different worker re-claimed between snapshot and reset; refuse."""
        conn = queue.connect(self.tmp)
        try:
            eid = self._running_event(conn, locked_by="worker-B")
            ok = queue.reset_running_to_queued(
                conn, eid, expected_locked_by="worker-A"
            )
            self.assertFalse(ok)
            row = conn.execute(
                "SELECT status, locked_by FROM events WHERE id=?", (eid,)
            ).fetchone()
            self.assertEqual(row["status"], "running")
            self.assertEqual(row["locked_by"], "worker-B")
        finally:
            conn.close()

    def test_no_expectation_resets_unconditionally(self) -> None:
        """When expected_locked_by is None, behave like the legacy path."""
        conn = queue.connect(self.tmp)
        try:
            eid = self._running_event(conn, locked_by="any")
            ok = queue.reset_running_to_queued(conn, eid)
            self.assertTrue(ok)
        finally:
            conn.close()


class CompleteFailStatusGuardTests(unittest.TestCase):
    """Bug #4 — complete/fail must refuse to overwrite a row that's no longer
    the caller's claim. Triggered by passing ``expected_locked_by``."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jc-guard-test-"))
        (self.tmp / ".jc").write_text("", encoding="utf-8")

    def _running_event(self, conn, *, locked_by: str = "worker-1") -> int:
        cur = conn.execute(
            """
            INSERT INTO events
              (source, content, meta, status, received_at, available_at, started_at,
               locked_by, locked_until)
            VALUES ('telegram', 'x', '{}', 'running', '2026-05-17T10:00:00+00:00',
                    '2026-05-17T10:00:00+00:00', '2026-05-17T10:00:00+00:00',
                    ?, '2026-05-17T10:05:00+00:00')
            """,
            (locked_by,),
        )
        conn.commit()
        return cur.lastrowid

    def test_complete_with_matching_locked_by_succeeds(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._running_event(conn, locked_by="worker-A")
            ev = queue.complete(
                conn, eid, response="ok", expected_locked_by="worker-A"
            )
            self.assertEqual(ev.status, "done")
            self.assertEqual(ev.response, "ok")
        finally:
            conn.close()

    def test_complete_refuses_when_locked_by_changed(self) -> None:
        """Stale worker A tries to complete a row now claimed by worker B."""
        conn = queue.connect(self.tmp)
        try:
            eid = self._running_event(conn, locked_by="worker-B")
            with self.assertRaises(KeyError):
                queue.complete(
                    conn, eid, response="stale", expected_locked_by="worker-A"
                )
            row = conn.execute(
                "SELECT status, response FROM events WHERE id=?", (eid,)
            ).fetchone()
            self.assertEqual(row["status"], "running")
            self.assertIsNone(row["response"])
        finally:
            conn.close()

    def test_complete_refuses_when_row_not_running(self) -> None:
        """Row was already completed by someone else; refuse."""
        conn = queue.connect(self.tmp)
        try:
            eid = self._running_event(conn, locked_by="worker-A")
            conn.execute(
                "UPDATE events SET status='done', locked_by=NULL WHERE id=?",
                (eid,),
            )
            conn.commit()
            with self.assertRaises(KeyError):
                queue.complete(
                    conn, eid, response="late", expected_locked_by="worker-A"
                )
        finally:
            conn.close()

    def test_fail_with_matching_locked_by_requeues(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._running_event(conn, locked_by="worker-A")
            ev = queue.fail(
                conn, eid, error="boom",
                max_retries=3, expected_locked_by="worker-A",
            )
            self.assertEqual(ev.status, "queued")
            self.assertEqual(ev.retry_count, 1)
        finally:
            conn.close()

    def test_fail_refuses_when_locked_by_changed(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._running_event(conn, locked_by="worker-B")
            with self.assertRaises(KeyError):
                queue.fail(
                    conn, eid, error="stale",
                    max_retries=3, expected_locked_by="worker-A",
                )
            row = conn.execute(
                "SELECT status, error FROM events WHERE id=?", (eid,)
            ).fetchone()
            self.assertEqual(row["status"], "running")
            self.assertIsNone(row["error"])
        finally:
            conn.close()

    def test_fail_without_expectation_is_unguarded(self) -> None:
        """Legacy fixture path (no expected_locked_by) still works on queued
        rows — used by watchdog tests that fail-fast a never-claimed event."""
        conn = queue.connect(self.tmp)
        try:
            cur = conn.execute(
                """
                INSERT INTO events (source, content, status, received_at, available_at)
                VALUES ('telegram', 'x', 'queued', '2026-05-17T10:00:00+00:00',
                        '2026-05-17T10:00:00+00:00')
                """
            )
            conn.commit()
            eid = cur.lastrowid
            ev = queue.fail(conn, eid, error="auth", max_retries=0)
            self.assertEqual(ev.status, "failed")
        finally:
            conn.close()


class RenewLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jc-renew-test-"))
        (self.tmp / ".jc").write_text("", encoding="utf-8")

    def _claim_one(self, conn, *, worker: str = "w-a", lease: int = 300):
        event, _ = queue.enqueue(
            conn,
            source="telegram",
            source_message_id="rl1",
            conversation_id="c",
            content="hi",
            user_id="u",
        )
        claimed = queue.claim_next(conn, worker_id=worker, lease_seconds=lease)
        assert claimed is not None and claimed.id == event.id
        return claimed

    def test_renew_extends_locked_until_for_owner(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            claimed = self._claim_one(conn, lease=60)
            original_until = claimed.locked_until
            # Renewal matches the per-claim token (event.locked_by), not the
            # bare worker id.
            renewed = queue.renew_lease(
                conn, claimed.id, worker_id=claimed.locked_by, lease_seconds=600
            )
            self.assertEqual(renewed, 1)
            row = conn.execute(
                "SELECT locked_until, status, locked_by FROM events WHERE id=?",
                (claimed.id,),
            ).fetchone()
            self.assertEqual(row["status"], "running")
            self.assertEqual(row["locked_by"], claimed.locked_by)
            self.assertNotEqual(row["locked_until"], original_until)
            self.assertGreater(row["locked_until"], original_until)
        finally:
            conn.close()

    def test_renew_refuses_when_not_owner(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            claimed = self._claim_one(conn, worker="w-a")
            renewed = queue.renew_lease(
                conn, claimed.id, worker_id="w-b", lease_seconds=300
            )
            self.assertEqual(renewed, 0)
            row = conn.execute(
                "SELECT locked_by FROM events WHERE id=?", (claimed.id,)
            ).fetchone()
            self.assertEqual(row["locked_by"], claimed.locked_by)
        finally:
            conn.close()

    def test_renew_refuses_after_complete(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            claimed = self._claim_one(conn)
            queue.complete(
                conn, claimed.id, response="ok", expected_locked_by=claimed.locked_by
            )
            renewed = queue.renew_lease(
                conn, claimed.id, worker_id=claimed.locked_by, lease_seconds=300
            )
            self.assertEqual(renewed, 0)
        finally:
            conn.close()

    def test_renew_after_lease_lost_to_other_worker(self) -> None:
        """Simulates the bug we're fixing: lease expires, another worker claims
        the row, and the original worker's heartbeat must NOT bump the new
        owner's lease."""
        conn = queue.connect(self.tmp)
        try:
            claimed = self._claim_one(conn, worker="w-a", lease=1)
            # Force expiry then re-claim by a different worker via the public API
            conn.execute(
                "UPDATE events SET locked_until=? WHERE id=?",
                ("2000-01-01T00:00:00Z", claimed.id),
            )
            conn.commit()
            new_claim = queue.claim_next(conn, worker_id="w-b", lease_seconds=300)
            self.assertIsNotNone(new_claim)
            assert new_claim is not None
            self.assertEqual(new_claim.id, claimed.id)
            # w-a now tries to heartbeat with its stale token — must be rejected.
            renewed = queue.renew_lease(
                conn, claimed.id, worker_id=claimed.locked_by, lease_seconds=300
            )
            self.assertEqual(renewed, 0)
            row = conn.execute(
                "SELECT locked_by FROM events WHERE id=?", (claimed.id,)
            ).fetchone()
            self.assertEqual(row["locked_by"], new_claim.locked_by)
        finally:
            conn.close()

    def test_renew_batch_renews_only_owned_rows(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            a, _ = queue.enqueue(
                conn, source="telegram", source_message_id="a",
                conversation_id="c1", content="a", user_id="u",
            )
            b, _ = queue.enqueue(
                conn, source="telegram", source_message_id="b",
                conversation_id="c1", content="b", user_id="u",
            )
            batch = queue.claim_batch_same_conversation(
                conn, worker_id="w-a", lease_seconds=60
            )
            self.assertEqual(len(batch), 2)
            token = batch[0].locked_by
            # Hand row b to a different worker by directly mutating.
            conn.execute(
                "UPDATE events SET locked_by='w-b#other' WHERE id=?", (b.id,)
            )
            conn.commit()
            renewed = queue.renew_lease(
                conn, [a.id, b.id], worker_id=token, lease_seconds=600
            )
            self.assertEqual(renewed, 1)
        finally:
            conn.close()

    def test_renew_empty_input_is_noop(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            self.assertEqual(
                queue.renew_lease(conn, [], worker_id="w", lease_seconds=300),
                0,
            )
        finally:
            conn.close()


class RequeueExpiredTests(unittest.TestCase):
    """Lease-expiry requeue must count retries so poison events can't loop
    forever (audit: crash→respawn→re-claim with ~lease_seconds period)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jc-requeue-test-"))
        (self.tmp / ".jc").write_text("", encoding="utf-8")

    def _expired_event(self, conn, *, retry_count: int = 0) -> int:
        cur = conn.execute(
            """
            INSERT INTO events
              (source, content, status, received_at, available_at, started_at,
               locked_by, locked_until, retry_count)
            VALUES ('telegram', 'x', 'running', '2026-05-17T10:00:00Z',
                    '2026-05-17T10:00:00Z', '2026-05-17T10:00:00Z',
                    'worker-1#deadbeef', '2026-05-17T10:05:00Z', ?)
            """,
            (retry_count,),
        )
        conn.commit()
        return cur.lastrowid

    def test_requeue_increments_retry_count(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._expired_event(conn, retry_count=0)
            requeued = queue.requeue_expired(conn)
            conn.commit()
            self.assertEqual(requeued, [eid])
            row = conn.execute(
                "SELECT status, retry_count, locked_by, error FROM events WHERE id=?",
                (eid,),
            ).fetchone()
            self.assertEqual(row["status"], "queued")
            self.assertEqual(row["retry_count"], 1)
            self.assertIsNone(row["locked_by"])
            self.assertEqual(row["error"], "lease expired")
        finally:
            conn.close()

    def test_poison_event_routed_to_failed_past_max_retries(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._expired_event(conn, retry_count=3)
            requeued = queue.requeue_expired(conn, max_retries=3)
            conn.commit()
            self.assertEqual(requeued, [])
            row = conn.execute(
                "SELECT status, retry_count, finished_at FROM events WHERE id=?",
                (eid,),
            ).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["retry_count"], 4)
            self.assertIsNotNone(row["finished_at"])
        finally:
            conn.close()

    def test_below_max_retries_still_requeues(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._expired_event(conn, retry_count=1)
            requeued = queue.requeue_expired(conn, max_retries=3)
            conn.commit()
            self.assertEqual(requeued, [eid])
            row = conn.execute(
                "SELECT status, retry_count FROM events WHERE id=?", (eid,)
            ).fetchone()
            self.assertEqual(row["status"], "queued")
            self.assertEqual(row["retry_count"], 2)
        finally:
            conn.close()

    def test_unexpired_lease_untouched(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            cur = conn.execute(
                """
                INSERT INTO events
                  (source, content, status, received_at, available_at, started_at,
                   locked_by, locked_until, retry_count)
                VALUES ('telegram', 'x', 'running', '2026-05-17T10:00:00Z',
                        '2026-05-17T10:00:00Z', '2026-05-17T10:00:00Z',
                        'worker-1', '2099-01-01T00:00:00Z', 0)
                """,
            )
            conn.commit()
            eid = cur.lastrowid
            self.assertEqual(queue.requeue_expired(conn), [])
            row = conn.execute(
                "SELECT status, retry_count FROM events WHERE id=?", (eid,)
            ).fetchone()
            self.assertEqual(row["status"], "running")
            self.assertEqual(row["retry_count"], 0)
        finally:
            conn.close()


class ClaimPathPoisonEscalationTests(unittest.TestCase):
    """Phase 3: the inline requeue inside the claim transaction must honor
    ``max_retries``. Without it, an expired poison row flips back to
    ``queued`` and is re-claimed in the SAME transaction, forever — the
    runtime's periodic requeue tick (which does pass ``max_retries``)
    never gets a look at it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jc-claimpoison-test-"))
        (self.tmp / ".jc").write_text("", encoding="utf-8")

    def _expired_event(
        self, conn, *, retry_count: int = 0, conversation_id: str = "conv-1"
    ) -> int:
        cur = conn.execute(
            """
            INSERT INTO events
              (source, content, status, received_at, available_at, started_at,
               locked_by, locked_until, retry_count, conversation_id)
            VALUES ('telegram', 'x', 'running', '2026-05-17T10:00:00Z',
                    '2026-05-17T10:00:00Z', '2026-05-17T10:00:00Z',
                    'worker-1#deadbeef', '2026-05-17T10:05:00Z', ?, ?)
            """,
            (retry_count, conversation_id),
        )
        conn.commit()
        return cur.lastrowid

    def test_claim_next_escalates_poison_row_to_failed(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._expired_event(conn, retry_count=3)
            event = queue.claim_next(conn, worker_id="w", max_retries=3)
            self.assertIsNone(event)  # NOT re-claimed
            row = conn.execute(
                "SELECT status, retry_count, error FROM events WHERE id=?",
                (eid,),
            ).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["retry_count"], 4)
            self.assertIn("max retries exceeded", row["error"])
        finally:
            conn.close()

    def test_claim_next_below_cap_requeues_and_reclaims(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._expired_event(conn, retry_count=1)
            event = queue.claim_next(conn, worker_id="w", max_retries=3)
            self.assertIsNotNone(event)
            self.assertEqual(event.id, eid)
            self.assertEqual(event.retry_count, 2)
            self.assertEqual(event.status, "running")
        finally:
            conn.close()

    def test_claim_next_without_max_retries_keeps_legacy_behavior(self) -> None:
        """CLI debug claims (cmd_claim/cmd_work_once) pass no max_retries —
        increment-only, never escalate."""
        conn = queue.connect(self.tmp)
        try:
            eid = self._expired_event(conn, retry_count=99)
            event = queue.claim_next(conn, worker_id="w")
            self.assertIsNotNone(event)
            self.assertEqual(event.id, eid)
            self.assertEqual(event.retry_count, 100)
        finally:
            conn.close()

    def test_claim_batch_escalates_poison_row_to_failed(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            eid = self._expired_event(conn, retry_count=3)
            batch = queue.claim_batch_same_conversation(
                conn, worker_id="w", max_retries=3
            )
            self.assertEqual(batch, [])
            row = conn.execute(
                "SELECT status, retry_count FROM events WHERE id=?", (eid,)
            ).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["retry_count"], 4)
        finally:
            conn.close()

    def test_claim_batch_poison_does_not_block_healthy_event(self) -> None:
        """A poison row routed to failed must not stop the claim from
        picking up the next healthy queued event in the same call."""
        conn = queue.connect(self.tmp)
        try:
            poison = self._expired_event(conn, retry_count=3)
            healthy, _ = queue.enqueue(
                conn,
                source="telegram",
                source_message_id="m-healthy",
                conversation_id="conv-2",
                content="hello",
            )
            conn.commit()
            batch = queue.claim_batch_same_conversation(
                conn, worker_id="w", max_retries=3
            )
            self.assertEqual([ev.id for ev in batch], [healthy.id])
            row = conn.execute(
                "SELECT status FROM events WHERE id=?", (poison,)
            ).fetchone()
            self.assertEqual(row["status"], "failed")
        finally:
            conn.close()


class MessageSlotsTests(unittest.TestCase):
    """message_slots map for deterministic slot routing
    (docs/specs/deterministic-slot-routing.md)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jc-queue-test-"))
        (self.tmp / ".jc").write_text("", encoding="utf-8")

    def test_schema_version_bumped(self) -> None:
        self.assertEqual(queue.SCHEMA_VERSION, 6)
        conn = queue.connect(self.tmp)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            self.assertEqual(row["value"], str(queue.SCHEMA_VERSION))
        finally:
            conn.close()

    def test_record_and_lookup_by_message(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            queue.record_message_slot(
                conn, channel="telegram", conversation_id="c1",
                message_id="m1", slot=2,
            )
            self.assertEqual(
                queue.slot_for_message(conn, channel="telegram", message_id="m1"), 2
            )
            self.assertIsNone(
                queue.slot_for_message(conn, channel="telegram", message_id="m-unknown")
            )
            # Channel is part of the key — no cross-channel bleed.
            self.assertIsNone(
                queue.slot_for_message(conn, channel="slack", message_id="m1")
            )
        finally:
            conn.close()

    def test_record_upserts_on_redispatch(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            queue.record_message_slot(
                conn, channel="telegram", conversation_id="c1",
                message_id="m1", slot=1,
            )
            queue.record_message_slot(
                conn, channel="telegram", conversation_id="c1",
                message_id="m1", slot=3,
            )
            self.assertEqual(
                queue.slot_for_message(conn, channel="telegram", message_id="m1"), 3
            )
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM message_slots"
            ).fetchone()["n"]
            self.assertEqual(count, 1)
        finally:
            conn.close()

    def test_latest_slot_for_conversation(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            self.assertIsNone(
                queue.latest_slot_for_conversation(
                    conn, channel="telegram", conversation_id="c1"
                )
            )
            queue.record_message_slot(
                conn, channel="telegram", conversation_id="c1",
                message_id="m1", slot=0,
            )
            queue.record_message_slot(
                conn, channel="telegram", conversation_id="c1",
                message_id="m2", slot=4,
            )
            # Same-second created_at — rowid tie-break picks the later insert.
            self.assertEqual(
                queue.latest_slot_for_conversation(
                    conn, channel="telegram", conversation_id="c1"
                ),
                4,
            )
            queue.record_message_slot(
                conn, channel="telegram", conversation_id="c2",
                message_id="m3", slot=1,
            )
            self.assertEqual(
                queue.latest_slot_for_conversation(
                    conn, channel="telegram", conversation_id="c1"
                ),
                4,
            )
        finally:
            conn.close()

    def test_prune_drops_old_rows(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            queue.record_message_slot(
                conn, channel="telegram", conversation_id="c1",
                message_id="m-old", slot=0,
            )
            conn.execute(
                "UPDATE message_slots SET created_at='2026-01-01T00:00:00Z' "
                "WHERE message_id='m-old'"
            )
            conn.commit()
            queue.record_message_slot(
                conn, channel="telegram", conversation_id="c1",
                message_id="m-new", slot=1,
            )
            removed = queue.prune_message_slots(conn, max_age_seconds=24 * 3600)
            self.assertEqual(removed, 1)
            self.assertIsNone(
                queue.slot_for_message(conn, channel="telegram", message_id="m-old")
            )
            self.assertEqual(
                queue.slot_for_message(conn, channel="telegram", message_id="m-new"), 1
            )
        finally:
            conn.close()

    def test_prune_caps_rows_per_conversation(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            for i in range(6):
                queue.record_message_slot(
                    conn, channel="telegram", conversation_id="c1",
                    message_id=f"m{i}", slot=i % 2,
                )
            # Other conversation stays untouched by c1's cap.
            queue.record_message_slot(
                conn, channel="telegram", conversation_id="c2",
                message_id="other", slot=0,
            )
            removed = queue.prune_message_slots(conn, keep_per_conversation=2)
            self.assertEqual(removed, 4)
            remaining = [
                row["message_id"]
                for row in conn.execute(
                    "SELECT message_id FROM message_slots "
                    "WHERE conversation_id='c1' ORDER BY rowid"
                ).fetchall()
            ]
            # Newest 2 survive (same-second timestamps → rowid order).
            self.assertEqual(remaining, ["m4", "m5"])
            self.assertEqual(
                queue.slot_for_message(conn, channel="telegram", message_id="other"), 0
            )
        finally:
            conn.close()

    def test_prune_noop_when_within_bounds(self) -> None:
        conn = queue.connect(self.tmp)
        try:
            queue.record_message_slot(
                conn, channel="telegram", conversation_id="c1",
                message_id="m1", slot=0,
            )
            self.assertEqual(queue.prune_message_slots(conn), 0)
            self.assertEqual(
                queue.slot_for_message(conn, channel="telegram", message_id="m1"), 0
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
