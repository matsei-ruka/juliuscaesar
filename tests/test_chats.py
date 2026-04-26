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


if __name__ == "__main__":
    unittest.main()
