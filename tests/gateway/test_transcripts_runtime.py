"""End-to-end transcript hooks: inbound enqueue + outbound delivery + resume priming."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue, transcripts  # noqa: E402
from gateway.brain import BrainResult  # noqa: E402
from gateway.brains.base import Brain  # noqa: E402
from gateway.config import render_default_config  # noqa: E402
from gateway.runtime import GatewayRuntime  # noqa: E402


def _make_instance() -> Path:
    root = Path(tempfile.mkdtemp(prefix="jc-transcripts-test-"))
    (root / ".jc").write_text("", encoding="utf-8")
    (root / "ops").mkdir()
    (root / "memory" / "L1").mkdir(parents=True)
    (root / "memory" / "L1" / "IDENTITY.md").write_text("Test", encoding="utf-8")
    (root / "ops" / "gateway.yaml").write_text(
        render_default_config(default_brain="claude"), encoding="utf-8"
    )
    return root


class TranscriptHooksTest(unittest.TestCase):
    def test_inbound_enqueue_writes_user_line(self) -> None:
        instance = _make_instance()
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        runtime.enqueue(
            source="telegram",
            source_message_id="m1",
            conversation_id="28547271",
            content="hello rachel",
            meta={"chat_id": "28547271"},
        )
        path = transcripts.transcript_path(instance, "28547271")
        self.assertTrue(path.exists())
        line = path.read_text(encoding="utf-8").strip()
        record = json.loads(line)
        self.assertEqual(record["role"], "user")
        self.assertEqual(record["text"], "hello rachel")
        self.assertEqual(record["channel"], "telegram")
        self.assertEqual(record["message_id"], "m1")
        runtime.close()

    def test_inbound_skips_non_chat_channels(self) -> None:
        instance = _make_instance()
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        runtime.enqueue(
            source="cron",
            source_message_id="cron-1",
            conversation_id="cron-conv",
            content="briefing",
        )
        self.assertFalse(transcripts.transcripts_dir(instance).exists())
        runtime.close()

    def test_outbound_writes_assistant_line_after_dispatch(self) -> None:
        instance = _make_instance()
        conn = queue.connect(instance)
        queue.enqueue(
            conn,
            source="telegram",
            source_message_id="m2",
            conversation_id="28547271",
            content="hi",
            meta={"chat_id": "28547271"},
        )
        conn.close()

        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        with mock.patch(
            "gateway.runtime.invoke_brain",
            return_value=BrainResult("hello back", "sess-1"),
        ), mock.patch("gateway.runtime.deliver_response", return_value="m-out"):
            self.assertTrue(runtime.dispatch_once())
        path = transcripts.transcript_path(instance, "28547271")
        events = list(transcripts.iter_events(path))
        roles = [e.role for e in events]
        texts = [e.text for e in events]
        # Inbound is written by enqueue(); we did the queue.enqueue directly,
        # bypassing runtime.enqueue, so only the outbound is recorded here.
        self.assertEqual(roles, ["assistant"])
        self.assertEqual(texts, ["hello back"])
        runtime.close()

    def test_full_round_trip_writes_user_then_assistant(self) -> None:
        instance = _make_instance()
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        runtime.enqueue(
            source="telegram",
            source_message_id="m3",
            conversation_id="28547271",
            content="ping",
            meta={"chat_id": "28547271"},
        )
        with mock.patch(
            "gateway.runtime.invoke_brain",
            return_value=BrainResult("pong", "sess-1"),
        ), mock.patch("gateway.runtime.deliver_response", return_value="m-out"):
            self.assertTrue(runtime.dispatch_once())
        path = transcripts.transcript_path(instance, "28547271")
        events = list(transcripts.iter_events(path))
        self.assertEqual([e.role for e in events], ["user", "assistant"])
        self.assertEqual([e.text for e in events], ["ping", "pong"])
        runtime.close()


class _DummyBrain(Brain):
    name = "dummy"
    needs_l1_preamble = True

    def __init__(self, instance_dir: Path) -> None:
        super().__init__(instance_dir)


class TranscriptPrimingTest(unittest.TestCase):
    def test_priming_block_excludes_current_inbound(self) -> None:
        instance = _make_instance()
        # Seed transcript with prior history + the just-arrived user message.
        for role, text in [
            ("user", "what is 2+2"),
            ("assistant", "4"),
            ("user", "and 3+3"),
        ]:
            transcripts.append(instance, conversation_id="c1", role=role, text=text)

        brain = _DummyBrain(instance)
        # Build a fake event that matches the trailing user line.
        event = queue.Event(
            id=99,
            source="telegram",
            source_message_id="m9",
            user_id="u1",
            conversation_id="c1",
            content="and 3+3",
            meta=None,
            status="queued",
            received_at="2026-04-29T10:00:00Z",
            available_at="2026-04-29T10:00:00Z",
            locked_by=None,
            locked_until=None,
            started_at=None,
            finished_at=None,
            retry_count=0,
            response=None,
            error=None,
        )
        priming = brain._build_transcript_priming(event)
        self.assertIn("what is 2+2", priming)
        self.assertIn("4", priming)
        # The trailing user line is the same as event.content; should be
        # trimmed so the priming doesn't duplicate the prompt's body.
        self.assertNotIn("and 3+3", priming)

    def test_priming_returns_empty_when_no_transcript(self) -> None:
        instance = _make_instance()
        brain = _DummyBrain(instance)
        event = queue.Event(
            id=1,
            source="telegram",
            source_message_id="m",
            user_id=None,
            conversation_id="ghost",
            content="hi",
            meta=None,
            status="queued",
            received_at="2026-04-29T10:00:00Z",
            available_at="2026-04-29T10:00:00Z",
            locked_by=None,
            locked_until=None,
            started_at=None,
            finished_at=None,
            retry_count=0,
            response=None,
            error=None,
        )
        self.assertEqual(brain._build_transcript_priming(event), "")


if __name__ == "__main__":
    unittest.main()
