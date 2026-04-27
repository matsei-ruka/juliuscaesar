"""Tests for the background reporter — batch shape, outbox buffering, replay."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from company import conf as company_conf  # noqa: E402
from company.client import CompanyError  # noqa: E402
from company.reporter import Outbox, Reporter, WorkersCursor, uuid7  # noqa: E402
from gateway import config as gw_config  # noqa: E402
from workers import db as workers_db  # noqa: E402


def _make_instance(tmp: str) -> Path:
    instance = Path(tmp)
    (instance / "ops").mkdir()
    (instance / "memory" / "L1").mkdir(parents=True)
    (instance / "ops" / "gateway.yaml").write_text(
        "default_brain: claude\n", encoding="utf-8"
    )
    (instance / ".env").write_text(
        "COMPANY_ENDPOINT=http://x\nCOMPANY_API_KEY=key\n", encoding="utf-8"
    )
    return instance


class Uuid7Tests(unittest.TestCase):
    def test_uuid7_format(self):
        uid = uuid7()
        self.assertEqual(len(uid), 36)
        # Version nibble is '7'.
        self.assertEqual(uid[14], "7")

    def test_uuid7_monotonic_per_ms(self):
        a = uuid7()
        time.sleep(0.002)
        b = uuid7()
        self.assertNotEqual(a, b)


class OutboxTests(unittest.TestCase):
    def test_append_and_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Outbox(Path(tmp), max_mb=10, max_age_hours=24)
            events = [
                {"event_type": "gateway.snapshot", "payload": {"queue_depth": 0}},
                {"event_type": "alert.raised", "payload": {"title": "x"}},
            ]
            outbox.append(events)
            files = outbox.files()
            self.assertEqual(len(files), 1)
            lines = files[0].read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            for line in lines:
                obj = json.loads(line)
                self.assertIn("queued_at", obj)

            sent: list[list[dict]] = []
            replayed = outbox.drain(lambda batch: sent.append(batch))
            self.assertEqual(replayed, 2)
            self.assertEqual(len(sent), 1)
            self.assertEqual(outbox.files(), [])

    def test_drain_keeps_file_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Outbox(Path(tmp), max_mb=10, max_age_hours=24)
            outbox.append([{"event_type": "x", "payload": {}}])

            def boom(batch):
                raise RuntimeError("network down")

            replayed = outbox.drain(boom)
            self.assertEqual(replayed, 0)
            # File still present so next tick retries.
            self.assertEqual(len(outbox.files()), 1)

    def test_drain_partial_failure_keeps_only_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Outbox(Path(tmp), max_mb=10, max_age_hours=24)
            events = [{"event_type": "x", "payload": {"i": i}} for i in range(1500)]
            outbox.append(events)

            calls: list[int] = []

            def send(batch):
                calls.append(len(batch))
                if len(calls) == 3:
                    raise RuntimeError("network down")

            replayed = outbox.drain(send, batch=500)
            self.assertEqual(replayed, 1000)
            # The file must now hold only the unsent tail (chunk 3 = 500 events).
            files = outbox.files()
            self.assertEqual(len(files), 1)
            lines = files[0].read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 500)
            first = json.loads(lines[0])
            self.assertEqual(first["payload"]["i"], 1000)

            # Next drain succeeds — no duplicates of the first 1000.
            calls.clear()
            sent: list[list[dict]] = []

            def send_ok(batch):
                sent.append(batch)

            replayed2 = outbox.drain(send_ok, batch=500)
            self.assertEqual(replayed2, 500)
            self.assertEqual(outbox.files(), [])
            self.assertEqual(sent[0][0]["payload"]["i"], 1000)

    def test_trim_evicts_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Outbox(Path(tmp), max_mb=10, max_age_hours=1)
            outbox.append([{"event_type": "x", "payload": {}}])
            old = outbox.files()[0]
            # Backdate mtime past age cap.
            two_hours_ago = time.time() - 2 * 3600
            import os

            os.utime(old, (two_hours_ago, two_hours_ago))

            dropped, _ = outbox.trim()
            self.assertGreaterEqual(dropped, 1)
            self.assertEqual(outbox.files(), [])

    def test_trim_evicts_oldest_until_under_size_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Tiny size cap so any single line trips eviction.
            # Note: cap is in MB so we can't easily bypass with a real value;
            # exercise the code path by manually shrinking max_bytes.
            outbox = Outbox(Path(tmp), max_mb=1, max_age_hours=24)
            outbox.max_bytes = 10  # extreme cap for the test
            outbox.append([{"event_type": "x", "payload": {"a": 1}}])
            outbox.append([{"event_type": "y", "payload": {"b": 2}}])
            outbox.trim()
            self.assertLessEqual(outbox.total_bytes(), 10)


class WorkersCursorTests(unittest.TestCase):
    def test_diff_emits_started_then_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            conn = workers_db.connect(instance)
            try:
                wid = workers_db.create(
                    conn,
                    topic="t",
                    brain="claude",
                    prompt_path="/tmp/p",
                    log_path="/tmp/l",
                    name="worker-1",
                )
            finally:
                conn.close()

            cursor = WorkersCursor(instance, boot_id="boot-A")
            first = cursor.diff(instance)
            kinds = [k for k, _ in first]
            self.assertEqual(kinds, ["worker.started"])
            self.assertEqual(first[0][1]["remote_id"], wid)
            self.assertEqual(first[0][1]["topic"], "t")

            # No change: empty diff.
            self.assertEqual(cursor.diff(instance), [])

            # Mark terminal: emits worker.finished only.
            conn = workers_db.connect(instance)
            try:
                workers_db.mark_terminal(conn, wid, status="done", exit_code=0)
            finally:
                conn.close()

            second = cursor.diff(instance)
            kinds2 = [k for k, _ in second]
            self.assertEqual(kinds2, ["worker.finished"])
            self.assertEqual(second[0][1]["status"], "done")

    def test_diff_resets_on_boot_id_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)

            # First boot: a worker exists, cursor records it.
            conn = workers_db.connect(instance)
            try:
                workers_db.create(
                    conn,
                    topic="t",
                    brain="claude",
                    prompt_path="/tmp/p",
                    log_path="/tmp/l",
                )
            finally:
                conn.close()

            cursor_a = WorkersCursor(instance, boot_id="boot-A")
            first = cursor_a.diff(instance)
            self.assertEqual([k for k, _ in first], ["worker.started"])

            # Wipe the workers DB to simulate a state reset; SQLite autoincrement
            # means the next create() yields id=1 again, colliding with the
            # cursor entry that was persisted under boot-A.
            (instance / "state" / "workers.db").unlink()
            conn = workers_db.connect(instance)
            try:
                wid = workers_db.create(
                    conn,
                    topic="t",
                    brain="claude",
                    prompt_path="/tmp/p",
                    log_path="/tmp/l",
                )
            finally:
                conn.close()
            self.assertEqual(wid, 1)

            # New cursor with a new boot_id must ignore the prior cache and
            # emit worker.started for the fresh row.
            cursor_b = WorkersCursor(instance, boot_id="boot-B")
            second = cursor_b.diff(instance)
            self.assertEqual([k for k, _ in second], ["worker.started"])

    def test_first_sight_terminal_emits_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            conn = workers_db.connect(instance)
            try:
                wid = workers_db.create(
                    conn,
                    topic="t",
                    brain="claude",
                    prompt_path="/tmp/p",
                    log_path="/tmp/l",
                )
                workers_db.mark_terminal(conn, wid, status="done", exit_code=0)
            finally:
                conn.close()

            cursor = WorkersCursor(instance, boot_id="boot-A")
            evts = cursor.diff(instance)
            kinds = [k for k, _ in evts]
            self.assertEqual(kinds, ["worker.started", "worker.finished"])


class TickTests(unittest.TestCase):
    def setUp(self) -> None:
        gw_config.clear_env_cache()

    def _build_reporter(self, instance: Path) -> tuple[Reporter, MagicMock]:
        reporter = Reporter(instance)
        fake = MagicMock()
        reporter.client = fake
        return reporter, fake

    def test_tick_posts_snapshot_and_worker_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            conn = workers_db.connect(instance)
            try:
                wid = workers_db.create(
                    conn,
                    topic="hello",
                    brain="claude",
                    prompt_path="/tmp/p",
                    log_path="/tmp/l",
                )
            finally:
                conn.close()

            reporter, fake = self._build_reporter(instance)
            reporter._tick()
            fake.post_events.assert_called_once()
            sent_batch = fake.post_events.call_args.args[0]
            event_types = [evt["event_type"] for evt in sent_batch]
            self.assertIn("gateway.snapshot", event_types)
            self.assertIn("worker.started", event_types)
            # Verify instance_boot_id is attached to worker events.
            for evt in sent_batch:
                if evt["event_type"].startswith("worker."):
                    self.assertEqual(evt["payload"]["instance_boot_id"], reporter.instance_boot_id)
                    self.assertEqual(evt["payload"]["remote_id"], wid)

    def test_tick_buffers_to_outbox_on_post_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            reporter, fake = self._build_reporter(instance)
            fake.post_events.side_effect = CompanyError("boom", status=502)
            reporter._tick()
            self.assertEqual(len(reporter.outbox.files()), 1)

    def test_unauthenticated_tick_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "ops").mkdir()
            (instance / "memory" / "L1").mkdir(parents=True)
            (instance / "ops" / "gateway.yaml").write_text(
                "default_brain: claude\n", encoding="utf-8"
            )
            # Endpoint set, but no api_key — reporter should buffer or skip.
            (instance / ".env").write_text(
                "COMPANY_ENDPOINT=http://x\nCOMPANY_ENROLLMENT_TOKEN=t\n",
                encoding="utf-8",
            )
            reporter, fake = self._build_reporter(instance)
            # Force registration to fail so the unauthenticated path runs.
            fake.register.side_effect = CompanyError("offline", status=0)
            reporter._tick()
            fake.post_events.assert_not_called()


class ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        gw_config.clear_env_cache()

    def test_post_events_partial_rejected_writes_dlq(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            reporter = Reporter(instance)
            fake = MagicMock()
            fake.post_events.return_value = {
                "accepted": 1,
                "rejected": [{"index": 0, "reason": "schema-violation"}],
            }
            reporter.client = fake

            chunk = [
                {"event_type": "x", "payload": {"i": 0}},
                {"event_type": "y", "payload": {"i": 1}},
            ]
            reporter._send_or_buffer(chunk)

            dlq_dir = instance / "state" / "company" / "dlq"
            files = list(dlq_dir.glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            lines = files[0].read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            row = json.loads(lines[0])
            self.assertEqual(row["payload"]["i"], 0)
            self.assertEqual(row["rejected_reason"], "schema-violation")
            # Outbox must NOT receive rejected events.
            self.assertEqual(reporter.outbox.files(), [])


class StopTests(unittest.TestCase):
    def setUp(self) -> None:
        gw_config.clear_env_cache()

    def test_stop_joins_thread_before_post_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            reporter = Reporter(instance)
            order: list[str] = []

            fake_client = MagicMock()
            fake_client.post_offline.side_effect = lambda snap: order.append(
                "post_offline"
            )
            fake_client.close.side_effect = lambda: order.append("close")
            reporter.client = fake_client

            fake_thread = MagicMock()
            fake_thread.is_alive.return_value = True
            fake_thread.join.side_effect = lambda timeout=None: order.append("join")
            reporter._thread = fake_thread

            reporter.stop()

            self.assertEqual(order, ["join", "post_offline", "close"])
            self.assertTrue(reporter._stop.is_set())
            # Snapshot dict was passed to post_offline.
            args, _ = fake_client.post_offline.call_args
            self.assertIsInstance(args[0], dict)


class ClientHeartbeatBodyTests(unittest.TestCase):
    def setUp(self) -> None:
        gw_config.clear_env_cache()

    def test_post_offline_body_matches_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            cfg = company_conf.load(instance)
            session = MagicMock()
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b"{}"
            resp.json.return_value = {}
            session.post.return_value = resp

            from company.client import CompanyClient
            client = CompanyClient(cfg, session=session)

            from company.reporter import build_snapshot
            client.post_offline(build_snapshot(instance))

            body = session.post.call_args.kwargs["json"]
            for key in (
                "status",
                "queue_depth",
                "brain_runtime",
                "triage_backend",
                "channels_enabled",
                "error_rate_5m",
                "cpu_pct",
                "memory_mb",
            ):
                self.assertIn(key, body)
            self.assertEqual(body["status"], "offline")


class UnauthenticatedTickTests(unittest.TestCase):
    def setUp(self) -> None:
        gw_config.clear_env_cache()

    def _build_unauth(self, tmp: str) -> tuple[Reporter, MagicMock]:
        instance = Path(tmp)
        (instance / "ops").mkdir()
        (instance / "memory" / "L1").mkdir(parents=True)
        (instance / "ops" / "gateway.yaml").write_text(
            "default_brain: claude\n", encoding="utf-8"
        )
        (instance / ".env").write_text(
            "COMPANY_ENDPOINT=http://x\nCOMPANY_ENROLLMENT_TOKEN=tok\n",
            encoding="utf-8",
        )
        reporter = Reporter(instance)
        reporter.REGISTER_RETRY_SECONDS = 0  # disable backoff for the test
        fake = MagicMock()
        fake.register.side_effect = CompanyError("offline", status=0)
        reporter.client = fake
        return reporter, fake

    def test_unauthenticated_conversations_buffer_to_outbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            reporter, _ = self._build_unauth(tmp)
            for i in range(3):
                event = MagicMock(
                    source="telegram",
                    user_id=str(i),
                    content=f"hi-{i}",
                    received_at="2026-04-27T10:00:00Z",
                )
                reporter.on_conversation(event, "ack", {"delivery_channel": "telegram"})
            reporter._tick()

            files = reporter.outbox.files()
            self.assertEqual(len(files), 1)
            lines = files[0].read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 6)  # 3 inbound + 3 outbound

    def test_register_retried_on_subsequent_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            reporter, fake = self._build_unauth(tmp)
            with patch("company.reporter.CompanyClient") as MockClient:
                # The replacement client (post-register) is a different mock
                # so we can verify the reporter swapped it in.
                new_client = MagicMock()
                MockClient.return_value = new_client

                # First tick — register raises.
                reporter._tick()
                self.assertEqual(reporter.cfg.api_key, "")

                # Second tick — register succeeds.
                fake.register.side_effect = None
                fake.register.return_value = {
                    "agent_id": "a",
                    "api_key": "fresh-key",
                }
                reporter._tick()

            self.assertEqual(reporter.cfg.api_key, "fresh-key")
            self.assertIs(reporter.client, new_client)


class ConversationHookTests(unittest.TestCase):
    def setUp(self) -> None:
        gw_config.clear_env_cache()

    def test_redacted_content_is_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            reporter = Reporter(instance)
            reporter.client = MagicMock()
            event = MagicMock(
                source="telegram",
                user_id="42",
                content="hello world",
                received_at="2026-04-27T10:00:00Z",
            )
            reporter.on_conversation(event, "hi back", {"delivery_channel": "telegram"})
            reporter._tick()
            sent = reporter.client.post_events.call_args.args[0]
            convs = [e for e in sent if e["event_type"] == "conversation.message"]
            self.assertEqual(len(convs), 2)  # inbound + outbound
            for evt in convs:
                self.assertTrue(evt["payload"]["content_redacted"])
                self.assertTrue(evt["payload"]["content"].startswith("sha256:"))
                self.assertNotIn("hello world", evt["payload"]["content"])

    def test_excluded_channel_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "ops").mkdir()
            (instance / "memory" / "L1").mkdir(parents=True)
            (instance / "ops" / "gateway.yaml").write_text(
                "default_brain: claude\ncompany:\n  exclude_channels: [voice]\n",
                encoding="utf-8",
            )
            (instance / ".env").write_text(
                "COMPANY_ENDPOINT=http://x\nCOMPANY_API_KEY=k\n", encoding="utf-8"
            )
            reporter = Reporter(instance)
            reporter.client = MagicMock()
            event = MagicMock(source="voice", user_id="42", content="hi")
            reporter.on_conversation(event, "ok", {"delivery_channel": "voice"})
            self.assertEqual(reporter._pending_events, [])

    def test_unredacted_content_is_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "ops").mkdir()
            (instance / "memory" / "L1").mkdir(parents=True)
            (instance / "ops" / "gateway.yaml").write_text(
                "default_brain: claude\ncompany:\n  redact_conversations: false\n  conversation_max_chars: 10\n",
                encoding="utf-8",
            )
            (instance / ".env").write_text(
                "COMPANY_ENDPOINT=http://x\nCOMPANY_API_KEY=k\n", encoding="utf-8"
            )
            reporter = Reporter(instance)
            reporter.client = MagicMock()
            event = MagicMock(
                source="telegram",
                user_id="42",
                content="this is a very long inbound message",
                received_at="2026-04-27T10:00:00Z",
            )
            reporter.on_conversation(event, "tiny", {"delivery_channel": "telegram"})
            inbound = reporter._pending_events[0]["payload"]
            self.assertFalse(inbound["content_redacted"])
            self.assertLessEqual(len(inbound["content"]), 10)


if __name__ == "__main__":
    unittest.main()
