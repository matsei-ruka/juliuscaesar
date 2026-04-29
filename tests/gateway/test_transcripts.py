"""Tests for lib/gateway/transcripts.py — append-only JSONL transcript log."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import transcripts  # noqa: E402


class TranscriptsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.instance_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_append_writes_jsonl_line(self) -> None:
        path = transcripts.append(
            self.instance_dir,
            conversation_id="28547271",
            role="user",
            text="hello world",
            message_id="42",
            channel="telegram",
            chat_id="28547271",
            ts="2026-04-29T10:32:48Z",
        )
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        body = path.read_text(encoding="utf-8").strip()
        record = json.loads(body)
        self.assertEqual(record["role"], "user")
        self.assertEqual(record["text"], "hello world")
        self.assertEqual(record["ts"], "2026-04-29T10:32:48Z")
        self.assertEqual(record["message_id"], "42")
        self.assertEqual(record["channel"], "telegram")

    def test_append_is_append_only(self) -> None:
        for n in range(3):
            transcripts.append(
                self.instance_dir,
                conversation_id="abc",
                role="user" if n % 2 == 0 else "assistant",
                text=f"msg-{n}",
            )
        path = transcripts.transcript_path(self.instance_dir, "abc")
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("msg-0", lines[0])
        self.assertIn("msg-1", lines[1])
        self.assertIn("msg-2", lines[2])

    def test_append_skips_when_no_conversation(self) -> None:
        out = transcripts.append(
            self.instance_dir,
            conversation_id=None,
            role="user",
            text="orphan",
        )
        self.assertIsNone(out)
        self.assertFalse(transcripts.transcripts_dir(self.instance_dir).exists())

    def test_append_skips_invalid_role(self) -> None:
        out = transcripts.append(
            self.instance_dir,
            conversation_id="x",
            role="system",
            text="nope",
        )
        self.assertIsNone(out)

    def test_append_safe_filename_for_unsafe_id(self) -> None:
        # Hostile id should not write outside the transcripts dir.
        path = transcripts.append(
            self.instance_dir,
            conversation_id="../etc/passwd",
            role="user",
            text="x",
        )
        self.assertIsNotNone(path)
        self.assertTrue(
            path.is_relative_to(transcripts.transcripts_dir(self.instance_dir))
        )

    def test_iter_events_skips_malformed_lines(self) -> None:
        path = transcripts.transcript_path(self.instance_dir, "c1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"ts":"2026-04-29T00:00:00Z","role":"user","text":"ok"}\n'
            "this is not json\n"
            '{"ts":"2026-04-29T00:01:00Z","role":"assistant","text":"hi"}\n',
            encoding="utf-8",
        )
        events = list(transcripts.iter_events(path))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].text, "ok")
        self.assertEqual(events[1].role, "assistant")

    def test_tail_returns_last_n(self) -> None:
        for n in range(5):
            transcripts.append(
                self.instance_dir,
                conversation_id="c",
                role="user",
                text=f"line-{n}",
            )
        path = transcripts.transcript_path(self.instance_dir, "c")
        events = transcripts.tail(path, lines=2)
        self.assertEqual([e.text for e in events], ["line-3", "line-4"])

    def test_search_filters_by_query_and_role(self) -> None:
        transcripts.append(self.instance_dir, conversation_id="c1", role="user", text="apple pie")
        transcripts.append(self.instance_dir, conversation_id="c1", role="assistant", text="apple sauce")
        transcripts.append(self.instance_dir, conversation_id="c2", role="user", text="banana")
        results = transcripts.search(self.instance_dir, "apple")
        self.assertEqual(len(results), 2)
        results_user = transcripts.search(self.instance_dir, "apple", role="user")
        self.assertEqual(len(results_user), 1)
        self.assertEqual(results_user[0][1].text, "apple pie")

    def test_search_filters_by_since(self) -> None:
        transcripts.append(
            self.instance_dir,
            conversation_id="c",
            role="user",
            text="old",
            ts="2026-04-20T00:00:00Z",
        )
        transcripts.append(
            self.instance_dir,
            conversation_id="c",
            role="user",
            text="new",
            ts="2026-04-29T00:00:00Z",
        )
        results = transcripts.search(self.instance_dir, "", since="2026-04-25T00:00:00Z")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1].text, "new")

    def test_get_by_message_id(self) -> None:
        transcripts.append(
            self.instance_dir,
            conversation_id="c",
            role="user",
            text="first",
            message_id="100",
        )
        transcripts.append(
            self.instance_dir,
            conversation_id="c",
            role="assistant",
            text="reply",
            message_id="101",
        )
        found = transcripts.get_by_message_id(self.instance_dir, "101")
        self.assertIsNotNone(found)
        path, ev = found
        self.assertEqual(ev.text, "reply")
        self.assertEqual(path.stem, "c")

    def test_render_priming_block(self) -> None:
        events = [
            transcripts.TranscriptEvent(
                ts="2026-04-29T10:00:00Z",
                role="user",
                text="hi",
            ),
            transcripts.TranscriptEvent(
                ts="2026-04-29T10:00:05Z",
                role="assistant",
                text="hello",
            ),
        ]
        block = transcripts.render_priming_block(events)
        self.assertIn("user: hi", block)
        self.assertIn("assistant: hello", block)
        self.assertIn("2026-04-29 10:00:00", block)


if __name__ == "__main__":
    unittest.main()
