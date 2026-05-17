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


if __name__ == "__main__":
    unittest.main()
