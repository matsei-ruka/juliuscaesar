"""Tests for the chat directory module."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import chats  # noqa: E402
from gateway import queue  # noqa: E402


class UpsertChatTests(unittest.TestCase):
    def test_first_seen_path_inserts_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            chat = chats.upsert_chat(
                instance,
                channel="telegram",
                chat_id="42",
                chat_type="private",
                title="Luca Mattei",
                username="luca",
                member_count=None,
                last_message_id="1001",
            )
            self.assertEqual(chat.channel, "telegram")
            self.assertEqual(chat.chat_id, "42")
            self.assertEqual(chat.chat_type, "private")
            self.assertEqual(chat.title, "Luca Mattei")
            self.assertEqual(chat.username, "luca")
            self.assertIsNone(chat.member_count)
            self.assertEqual(chat.last_message_id, "1001")
            self.assertEqual(chat.first_seen, chat.last_seen)

    def test_last_seen_path_preserves_first_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            initial = chats.upsert_chat(
                instance,
                channel="telegram",
                chat_id="42",
                title="Luca",
                last_message_id="1001",
            )
            # Sleep so the seconds-resolution timestamp can advance.
            time.sleep(1.1)
            updated = chats.upsert_chat(
                instance,
                channel="telegram",
                chat_id="42",
                title="Luca",
                last_message_id="1002",
            )
            self.assertEqual(updated.first_seen, initial.first_seen)
            self.assertGreater(updated.last_seen, initial.last_seen)
            self.assertEqual(updated.last_message_id, "1002")

    def test_null_fields_preserve_prior_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            chats.upsert_chat(
                instance,
                channel="telegram",
                chat_id="-1001",
                chat_type="supergroup",
                title="BNESIM ops",
                username="bnesim_ops",
                member_count=8,
                last_message_id="500",
            )
            # Subsequent update: only last_message_id; other fields nil.
            chats.upsert_chat(
                instance,
                channel="telegram",
                chat_id="-1001",
                last_message_id="501",
            )
            row = chats.get_chat(instance, channel="telegram", chat_id="-1001")
            self.assertIsNotNone(row)
            self.assertEqual(row.chat_type, "supergroup")
            self.assertEqual(row.title, "BNESIM ops")
            self.assertEqual(row.username, "bnesim_ops")
            self.assertEqual(row.member_count, 8)
            self.assertEqual(row.last_message_id, "501")


class ListChatsTests(unittest.TestCase):
    def test_list_orders_by_last_seen_desc(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            chats.upsert_chat(instance, channel="telegram", chat_id="A", title="A")
            time.sleep(1.1)
            chats.upsert_chat(instance, channel="telegram", chat_id="B", title="B")
            time.sleep(1.1)
            chats.upsert_chat(instance, channel="telegram", chat_id="C", title="C")
            rows = chats.list_chats(instance, channel="telegram")
            ids = [c.chat_id for c in rows]
            self.assertEqual(ids, ["C", "B", "A"])

    def test_list_filters_by_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            chats.upsert_chat(instance, channel="telegram", chat_id="A")
            chats.upsert_chat(instance, channel="discord", chat_id="X")
            telegram = chats.list_chats(instance, channel="telegram")
            self.assertEqual([c.chat_id for c in telegram], ["A"])
            discord = chats.list_chats(instance, channel="discord")
            self.assertEqual([c.chat_id for c in discord], ["X"])

    def test_list_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            for cid in ("A", "B", "C", "D"):
                chats.upsert_chat(instance, channel="telegram", chat_id=cid)
                time.sleep(1.05)
            rows = chats.list_chats(instance, channel="telegram", limit=2)
            self.assertEqual(len(rows), 2)
            # Most-recent two: D, C.
            self.assertEqual([c.chat_id for c in rows], ["D", "C"])

    def test_list_no_filter_returns_all_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            chats.upsert_chat(instance, channel="telegram", chat_id="A")
            chats.upsert_chat(instance, channel="discord", chat_id="X")
            rows = chats.list_chats(instance)
            self.assertEqual(len(rows), 2)


class PruneChatsTests(unittest.TestCase):
    def test_prune_deletes_old_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            chats.upsert_chat(instance, channel="telegram", chat_id="old")
            # Fast-forward this row's last_seen by hand to simulate age.
            conn = queue.connect(instance)
            try:
                conn.execute(
                    "UPDATE chats SET last_seen='2020-01-01T00:00:00Z' "
                    "WHERE chat_id='old'"
                )
                conn.commit()
            finally:
                conn.close()
            chats.upsert_chat(instance, channel="telegram", chat_id="new")
            removed = chats.prune_chats(instance, older_than_days=30)
            self.assertEqual(removed, 1)
            remaining = chats.list_chats(instance, channel="telegram")
            self.assertEqual([c.chat_id for c in remaining], ["new"])


class SharedConnectionTests(unittest.TestCase):
    def test_upsert_with_shared_conn_does_not_open_extra_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            conn = queue.connect(instance)
            try:
                opens = {"count": 0}
                orig_connect = queue.connect

                def counting_connect(p):
                    opens["count"] += 1
                    return orig_connect(p)

                # Monkey-patch the chats module's reference too.
                from gateway import chats as chats_mod

                chats_mod.queue.connect = counting_connect
                try:
                    for i in range(3):
                        chats.upsert_chat(
                            conn=conn,
                            channel="telegram",
                            chat_id=f"c{i}",
                            title=f"Chat {i}",
                        )
                finally:
                    chats_mod.queue.connect = orig_connect
                self.assertEqual(
                    opens["count"], 0,
                    "shared conn must avoid opening fresh connections",
                )
                rows = chats.list_chats(conn=conn, channel="telegram")
                self.assertEqual({c.chat_id for c in rows}, {"c0", "c1", "c2"})
            finally:
                conn.close()

    def test_serial_burst_with_shared_conn_no_extra_opens(self):
        """The Telegram channel runs single-threaded; a 100-message burst on a
        cached conn must not open one connection per call."""
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            conn = queue.connect(instance)
            try:
                opens = {"count": 0}
                from gateway import chats as chats_mod

                orig_connect = queue.connect

                def counting_connect(p):
                    opens["count"] += 1
                    return orig_connect(p)

                chats_mod.queue.connect = counting_connect
                try:
                    for j in range(100):
                        chats.upsert_chat(
                            conn=conn,
                            channel="telegram",
                            chat_id="-100",
                            title="loop",
                            last_message_id=str(j),
                        )
                finally:
                    chats_mod.queue.connect = orig_connect
                self.assertEqual(opens["count"], 0)
                row = chats.get_chat(conn=conn, channel="telegram", chat_id="-100")
                self.assertEqual(row.last_message_id, "99")
            finally:
                conn.close()

    def test_two_separate_conns_with_busy_timeout_serialize(self):
        """Two connections from the same thread can both write — busy_timeout
        + WAL serialize them. Smoke check that we don't immediately throw
        SQLITE_BUSY for trivial alternation."""
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            conn_a = queue.connect(instance)
            conn_b = queue.connect(instance)
            try:
                for i in range(10):
                    chats.upsert_chat(
                        conn=conn_a,
                        channel="telegram",
                        chat_id="A",
                        last_message_id=str(i),
                    )
                    chats.upsert_chat(
                        conn=conn_b,
                        channel="telegram",
                        chat_id="B",
                        last_message_id=str(i),
                    )
                rows = chats.list_chats(conn=conn_a, channel="telegram")
                self.assertEqual({c.chat_id for c in rows}, {"A", "B"})
            finally:
                conn_a.close()
                conn_b.close()


class AuthStatusTests(unittest.TestCase):
    def test_default_auth_status_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            chat = chats.upsert_chat(instance, channel="telegram", chat_id="1")
            self.assertEqual(chat.auth_status, "pending")

    def test_upsert_with_explicit_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            chat = chats.upsert_chat(
                instance,
                channel="telegram",
                chat_id="-100",
                auth_status="pending",
            )
            self.assertEqual(chat.auth_status, "pending")

    def test_upsert_invalid_auth_raises(self):
        with self.assertRaises(ValueError):
            chats.upsert_chat(
                Path("/tmp/x"),
                channel="telegram",
                chat_id="1",
                auth_status="bogus",
            )

    def test_set_auth_status_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            chats.upsert_chat(instance, channel="telegram", chat_id="-100")
            updated = chats.set_auth_status(
                instance,
                channel="telegram",
                chat_id="-100",
                status="denied",
            )
            self.assertIsNotNone(updated)
            self.assertEqual(updated.auth_status, "denied")
            current = chats.get_chat(instance, channel="telegram", chat_id="-100")
            self.assertEqual(current.auth_status, "denied")

    def test_set_auth_status_missing_chat_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            result = chats.set_auth_status(
                instance,
                channel="telegram",
                chat_id="missing",
                status="allowed",
            )
            self.assertIsNone(result)

    def test_subsequent_upsert_preserves_auth_status_when_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            chats.upsert_chat(
                instance,
                channel="telegram",
                chat_id="-100",
                auth_status="pending",
            )
            # Refresh without specifying auth_status — must NOT clobber.
            chats.upsert_chat(
                instance,
                channel="telegram",
                chat_id="-100",
                last_message_id="500",
            )
            row = chats.get_chat(instance, channel="telegram", chat_id="-100")
            self.assertEqual(row.auth_status, "pending")

    def test_pending_chats_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            chats.upsert_chat(
                instance, channel="telegram", chat_id="A", auth_status="allowed"
            )
            chats.upsert_chat(
                instance, channel="telegram", chat_id="B", auth_status="pending"
            )
            chats.upsert_chat(
                instance, channel="telegram", chat_id="C", auth_status="denied"
            )
            pending = chats.pending_chats(instance, channel="telegram")
            self.assertEqual([c.chat_id for c in pending], ["B"])


class L1ChatsGeneratorTests(unittest.TestCase):
    def test_regenerate_writes_file_with_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "memory" / "L1").mkdir(parents=True)
            chats.upsert_chat(
                instance,
                channel="telegram",
                chat_id="42",
                chat_type="private",
                title="Luca Mattei",
                username="luca",
            )
            path = chats.regenerate_l1_chats(instance)
            self.assertIsNotNone(path)
            self.assertEqual(path.name, "CHATS.md")
            text = path.read_text(encoding="utf-8")
            self.assertIn("AUTO-GENERATED", text)
            self.assertIn("Luca Mattei", text)
            self.assertIn("42", text)

    def test_regenerate_skips_when_l1_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            chats.upsert_chat(instance, channel="telegram", chat_id="42")
            self.assertIsNone(chats.regenerate_l1_chats(instance))

    def test_upsert_triggers_debounced_regen(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "memory" / "L1").mkdir(parents=True)
            # Reset debounce state for a clean test.
            chats._LAST_REGEN.clear()
            chats.upsert_chat(
                instance,
                channel="telegram",
                chat_id="42",
                title="Luca",
            )
            target = instance / "memory" / "L1" / "CHATS.md"
            self.assertTrue(target.exists())
            text_before = target.read_text(encoding="utf-8")

            # Second upsert within debounce window — file must NOT be rewritten.
            target.write_text("STALE", encoding="utf-8")
            chats.upsert_chat(
                instance,
                channel="telegram",
                chat_id="43",
                title="Other",
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "STALE")
            del text_before


class MemoryRebuildSkipsChatsTests(unittest.TestCase):
    def test_rebuild_skips_auto_generated_chats_file(self):
        from memory import db as memory_db

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "memory" / "L1").mkdir(parents=True)
            (instance / "memory" / "L1" / "IDENTITY.md").write_text(
                "---\nslug: IDENTITY\ntitle: Test\nlayer: L1\nstate: draft\n---\n\nbody",
                encoding="utf-8",
            )
            chats.upsert_chat(instance, channel="telegram", chat_id="1", title="x")
            chats.regenerate_l1_chats(instance)
            self.assertTrue((instance / "memory" / "L1" / "CHATS.md").exists())

            paths = list(memory_db._iter_md_files(instance))
            names = {p.name for p in paths}
            self.assertNotIn("CHATS.md", names)
            self.assertIn("IDENTITY.md", names)


if __name__ == "__main__":
    unittest.main()
