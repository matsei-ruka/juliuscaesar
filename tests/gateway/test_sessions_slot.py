"""Tests for the per-slot session table introduced with parallel-slots.

Covers:
- backward-compat: omitting `slot` defaults to 0 (same SQL as before).
- new behavior: distinct rows per slot for the same (channel, conv, brain).
- migration: a legacy schema (no `slot` column) is auto-upgraded with row data
  preserved at slot 0.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import sessions  # noqa: E402


def _connect_tmp() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    sessions.init_db(conn)
    return conn


class SessionsSlotBasicTests(unittest.TestCase):
    def test_default_slot_zero_when_omitted(self) -> None:
        conn = _connect_tmp()
        try:
            saved = sessions.upsert_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
                session_id="s-abc",
            )
            self.assertEqual(saved.slot, 0)

            fetched = sessions.get_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
            )
            self.assertIsNotNone(fetched)
            assert fetched is not None
            self.assertEqual(fetched.slot, 0)
            self.assertEqual(fetched.session_id, "s-abc")
        finally:
            conn.close()

    def test_distinct_rows_per_slot(self) -> None:
        conn = _connect_tmp()
        try:
            sessions.upsert_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
                session_id="s-zero",
                slot=0,
            )
            sessions.upsert_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
                session_id="s-one",
                slot=1,
            )
            sessions.upsert_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
                session_id="s-two",
                slot=2,
            )

            zero = sessions.get_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
                slot=0,
            )
            one = sessions.get_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
                slot=1,
            )
            two = sessions.get_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
                slot=2,
            )
            assert zero and one and two
            self.assertEqual(zero.session_id, "s-zero")
            self.assertEqual(one.session_id, "s-one")
            self.assertEqual(two.session_id, "s-two")

            rows = sessions.list_sessions_for_conversation(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
            )
            self.assertEqual([r.slot for r in rows], [0, 1, 2])
        finally:
            conn.close()

    def test_upsert_updates_only_matching_slot(self) -> None:
        conn = _connect_tmp()
        try:
            sessions.upsert_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
                session_id="s-zero-v1",
                slot=0,
            )
            sessions.upsert_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
                session_id="s-one-v1",
                slot=1,
            )
            sessions.upsert_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
                session_id="s-one-v2",
                slot=1,
            )
            zero = sessions.get_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
                slot=0,
            )
            one = sessions.get_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
                slot=1,
            )
            assert zero and one
            self.assertEqual(zero.session_id, "s-zero-v1")
            self.assertEqual(one.session_id, "s-one-v2")
        finally:
            conn.close()

    def test_missing_slot_returns_none(self) -> None:
        conn = _connect_tmp()
        try:
            sessions.upsert_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
                session_id="s-zero",
                slot=0,
            )
            self.assertIsNone(
                sessions.get_session(
                    conn,
                    channel="telegram",
                    conversation_id="c1",
                    brain="claude",
                    slot=4,
                )
            )
        finally:
            conn.close()


class SessionsSlotMigrationTests(unittest.TestCase):
    """Existing tables without `slot` must be upgraded transparently."""

    def test_legacy_table_gets_slot_column_with_rows_preserved(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # Simulate the pre-parallel-slots schema.
        conn.executescript(
            """
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                brain TEXT NOT NULL,
                session_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(channel, conversation_id, brain)
            );
            INSERT INTO sessions (channel, conversation_id, brain, session_id, created_at, updated_at)
            VALUES ('telegram', 'c1', 'claude', 's-legacy', '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z');
            """
        )
        conn.commit()

        sessions.init_db(conn)

        cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        self.assertIn("slot", cols)

        row = sessions.get_session(
            conn,
            channel="telegram",
            conversation_id="c1",
            brain="claude",
            slot=0,
        )
        assert row is not None
        self.assertEqual(row.session_id, "s-legacy")
        self.assertEqual(row.slot, 0)

        # Slot >0 must be free, no carry-over.
        self.assertIsNone(
            sessions.get_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
                slot=1,
            )
        )

        # Re-running migration is a no-op.
        self.assertFalse(sessions._migrate_add_slot(conn))


if __name__ == "__main__":
    unittest.main()
