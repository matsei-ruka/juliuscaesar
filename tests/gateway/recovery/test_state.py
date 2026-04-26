"""auth_pending state machine — transitions, expiry, race."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue  # noqa: E402
from gateway.recovery import state  # noqa: E402


def _instance(tmp: str) -> Path:
    inst = Path(tmp)
    (inst / ".jc").write_text("", encoding="utf-8")
    return inst


class AuthPendingTests(unittest.TestCase):
    def test_insert_creates_waiting_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = _instance(tmp)
            conn = queue.connect(inst)
            try:
                row = state.insert_pending(
                    conn,
                    event_id=10,
                    operator_chat="42",
                    login_url="https://claude.ai/cli/auth?token=x",
                )
                self.assertIsNotNone(row)
                self.assertEqual(row.state, "waiting")
                self.assertEqual(row.event_id, 10)
            finally:
                conn.close()

    def test_unique_index_blocks_second_active_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = _instance(tmp)
            conn = queue.connect(inst)
            try:
                first = state.insert_pending(
                    conn, event_id=1, operator_chat="42", login_url="https://claude.ai/x"
                )
                second = state.insert_pending(
                    conn, event_id=2, operator_chat="42", login_url="https://claude.ai/y"
                )
                self.assertIsNotNone(first)
                self.assertIsNone(second)
            finally:
                conn.close()

    def test_append_pending_event_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = _instance(tmp)
            conn = queue.connect(inst)
            try:
                row = state.insert_pending(
                    conn, event_id=1, operator_chat="42", login_url="https://claude.ai/x"
                )
                state.append_pending_event(conn, pending_id=row.id, event_id=2)
                state.append_pending_event(conn, pending_id=row.id, event_id=2)  # dup
                state.append_pending_event(conn, pending_id=row.id, event_id=3)
                refreshed = state.get_by_id(conn, pending_id=row.id)
                self.assertEqual(refreshed.pending_events, [2, 3])
            finally:
                conn.close()

    def test_transition_to_done_releases_unique_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = _instance(tmp)
            conn = queue.connect(inst)
            try:
                first = state.insert_pending(
                    conn, event_id=1, operator_chat="42", login_url="x"
                )
                state.transition(conn, pending_id=first.id, new_state="done")
                second = state.insert_pending(
                    conn, event_id=2, operator_chat="42", login_url="y"
                )
                self.assertIsNotNone(second)
                self.assertNotEqual(first.id, second.id)
            finally:
                conn.close()

    def test_expire_old_marks_stale_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = _instance(tmp)
            conn = queue.connect(inst)
            try:
                state.init_table(conn)
                # Insert directly with a past expires_at.
                conn.execute(
                    """
                    INSERT INTO auth_pending(
                        event_id, operator_chat, login_url,
                        requested_at, expires_at, state
                    ) VALUES (1, '42', 'x', '2020-01-01T00:00:00Z',
                              '2020-01-01T00:10:00Z', 'waiting')
                    """,
                )
                conn.commit()
                expired = state.expire_old(conn)
                self.assertEqual(len(expired), 1)
                row = conn.execute("SELECT state FROM auth_pending").fetchone()
                self.assertEqual(row["state"], "expired")
            finally:
                conn.close()

    def test_get_active_pending_excludes_done_and_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = _instance(tmp)
            conn = queue.connect(inst)
            try:
                row = state.insert_pending(
                    conn, event_id=1, operator_chat="42", login_url="x"
                )
                state.transition(conn, pending_id=row.id, new_state="done")
                self.assertIsNone(
                    state.get_active_pending(conn, operator_chat="42")
                )
            finally:
                conn.close()


class TokenHelpersTests(unittest.TestCase):
    def test_looks_like_token_minimum_length(self):
        self.assertFalse(state.looks_like_token("short"))
        self.assertTrue(state.looks_like_token("a" * 25))

    def test_looks_like_token_rejects_whitespace(self):
        self.assertFalse(state.looks_like_token("hello world is a long phrase"))
        self.assertFalse(state.looks_like_token("with\nnewline-in-the-middle-here"))

    def test_looks_like_token_allows_dot_dash_underscore(self):
        self.assertTrue(state.looks_like_token("sk-abc.def_ghi-jkl_12345678"))

    def test_fingerprint_is_stable_and_short(self):
        fp1 = state.fingerprint_token("sk-1234567890abcdef")
        fp2 = state.fingerprint_token("sk-1234567890abcdef")
        self.assertEqual(fp1, fp2)
        self.assertNotIn("1234567890", fp1)
        self.assertEqual(fp1.split("…", 1)[0], "sk-1")

    def test_fingerprint_handles_empty(self):
        self.assertEqual(state.fingerprint_token(""), "(empty)")


if __name__ == "__main__":
    unittest.main()
