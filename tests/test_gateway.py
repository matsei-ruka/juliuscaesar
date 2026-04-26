import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gateway import queue
from gateway.brain import BrainResult
from gateway.config import load_config, render_default_config
from gateway.runtime import GatewayRuntime


class GatewayTests(unittest.TestCase):
    def make_instance(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="jc-gateway-test-"))
        (root / ".jc").write_text("", encoding="utf-8")
        (root / "ops").mkdir()
        (root / "memory" / "L1").mkdir(parents=True)
        (root / "memory" / "L1" / "IDENTITY.md").write_text("Julius test", encoding="utf-8")
        (root / "ops" / "gateway.yaml").write_text(
            render_default_config(default_brain="claude"), encoding="utf-8"
        )
        return root

    def test_queue_dedup_and_sessions(self):
        instance = self.make_instance()
        conn = queue.connect(instance)
        try:
            first, inserted = queue.enqueue(
                conn,
                source="telegram",
                source_message_id="u1",
                conversation_id="c1",
                content="hello",
            )
            second, inserted_again = queue.enqueue(
                conn,
                source="telegram",
                source_message_id="u1",
                conversation_id="c1",
                content="hello again",
            )
            self.assertTrue(inserted)
            self.assertFalse(inserted_again)
            self.assertEqual(first.id, second.id)

            queue.upsert_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
                session_id="s1",
            )
            session = queue.get_session(
                conn,
                channel="telegram",
                conversation_id="c1",
                brain="claude",
            )
            self.assertIsNotNone(session)
            self.assertEqual(session.session_id, "s1")
        finally:
            conn.close()

    def test_config_loads_generated_yaml_without_pyyaml_assumptions(self):
        instance = self.make_instance()
        (instance / "ops" / "gateway.yaml").write_text(
            render_default_config(
                default_brain="gemini",
                telegram_enabled=True,
                telegram_chat_id="123",
                slack_enabled=True,
            ),
            encoding="utf-8",
        )
        cfg = load_config(instance)
        self.assertEqual(cfg.default_brain, "gemini")
        self.assertTrue(cfg.channel("telegram").enabled)
        self.assertEqual(cfg.channel("telegram").chat_ids, ("123",))
        self.assertTrue(cfg.channel("slack").enabled)

    def test_dispatcher_success_stores_response_and_session(self):
        instance = self.make_instance()
        conn = queue.connect(instance)
        event, _ = queue.enqueue(
            conn,
            source="manual",
            conversation_id="manual-conv",
            content="hi",
        )
        conn.close()
        runtime = GatewayRuntime(instance, log_path=queue.queue_dir(instance) / "test.log", stop_requested=lambda: True)
        with mock.patch("gateway.runtime.invoke_brain", return_value=BrainResult("hello", "sess-1")), \
             mock.patch("gateway.runtime.deliver", return_value=None):
            self.assertTrue(runtime.dispatch_once())

        conn2 = queue.connect(instance)
        try:
            saved = queue.get(conn2, event.id)
            self.assertEqual(saved.status, "done")
            self.assertEqual(saved.response, "hello")
            session = queue.get_session(
                conn2,
                channel="manual",
                conversation_id="manual-conv",
                brain="claude",
            )
            self.assertEqual(session.session_id, "sess-1")
        finally:
            conn2.close()

    def test_chats_table_idempotent(self):
        instance = self.make_instance()
        conn = queue.connect(instance)
        try:
            cols = [
                row["name"]
                for row in conn.execute("PRAGMA table_info(chats)").fetchall()
            ]
            self.assertEqual(
                cols,
                [
                    "channel",
                    "chat_id",
                    "chat_type",
                    "title",
                    "username",
                    "member_count",
                    "first_seen",
                    "last_seen",
                    "last_message_id",
                ],
            )
            schema_version = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()["value"]
            self.assertEqual(schema_version, "3")
        finally:
            conn.close()

        # Second connect must be a no-op (no error, no duplicate table).
        conn2 = queue.connect(instance)
        try:
            count = conn2.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master "
                "WHERE type='table' AND name='chats'"
            ).fetchone()["n"]
            self.assertEqual(count, 1)
            idx = conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_chats_last_seen'"
            ).fetchone()
            self.assertIsNotNone(idx)
        finally:
            conn2.close()

    def test_dispatcher_failure_requeues(self):
        instance = self.make_instance()
        conn = queue.connect(instance)
        event, _ = queue.enqueue(conn, source="manual", content="hi")
        conn.close()
        runtime = GatewayRuntime(instance, log_path=queue.queue_dir(instance) / "test.log", stop_requested=lambda: True)
        with mock.patch("gateway.runtime.invoke_brain", side_effect=RuntimeError("boom")):
            self.assertTrue(runtime.dispatch_once())
        conn2 = queue.connect(instance)
        try:
            saved = queue.get(conn2, event.id)
            self.assertEqual(saved.status, "queued")
            self.assertEqual(saved.retry_count, 1)
            self.assertIn("boom", saved.error)
        finally:
            conn2.close()


if __name__ == "__main__":
    unittest.main()
