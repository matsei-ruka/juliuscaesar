"""Tests for Codex CLI session capture safety.

Covers docs/specs/codex-main-brain-hardening.md §Phase 4 acceptance:

- A session created by the gateway invocation is captured.
- Concurrent unrelated Codex JSONL created after gateway start is NOT captured.
- No session id -> capture returns None (runtime falls back to priming).

The previous implementation used a timestamp-only global scan against
`~/.codex/sessions/`, which could pick up an unrelated session id from a
concurrent Codex process. This was the spec's hard rule violation: never
resume a session id that cannot be tied to this gateway invocation.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.brains.codex import CodexBrain  # noqa: E402


class _CodexHome:
    """Context that points `Path.home()` at a temp dir with a `.codex/sessions/`."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.sessions = self.home / ".codex" / "sessions"
        self.sessions.mkdir(parents=True)
        self._patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})

    def __enter__(self):
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        self._tmp.cleanup()

    def write_session(self, name: str) -> Path:
        path = self.sessions / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        return path


class CodexSessionCaptureTests(unittest.TestCase):
    def test_captures_session_created_by_this_invocation(self):
        with _CodexHome() as home:
            home.write_session("rollout-2026-05-01T10-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl")
            brain = CodexBrain(Path.cwd())
            brain._pre_state = brain.pre_invoke_snapshot()  # snapshot pre-spawn

            new_uuid = "11111111-2222-3333-4444-555555555555"
            home.write_session(f"rollout-2026-05-01T10-{new_uuid}.jsonl")

            captured = brain.capture_session_id("2026-05-01T10:00:00Z")
            self.assertEqual(captured, new_uuid)

    def test_concurrent_unrelated_session_is_not_captured(self):
        with _CodexHome() as home:
            home.write_session("rollout-2026-05-01T10-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl")
            brain = CodexBrain(Path.cwd())
            brain._pre_state = brain.pre_invoke_snapshot()

            # Two new files appear during this invocation: one is ours, one
            # is a concurrent unrelated Codex process. We cannot tell them
            # apart safely, so capture must return None.
            home.write_session(f"rollout-2026-05-01T10-{'1' * 8}-2222-3333-4444-555555555555.jsonl")
            home.write_session(f"rollout-2026-05-01T10-{'9' * 8}-8888-7777-6666-555555555555.jsonl")

            captured = brain.capture_session_id("2026-05-01T10:00:00Z")
            self.assertIsNone(captured)

    def test_no_new_session_returns_none(self):
        with _CodexHome() as home:
            home.write_session("rollout-old-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl")
            brain = CodexBrain(Path.cwd())
            brain._pre_state = brain.pre_invoke_snapshot()
            # No new file written between snapshot and capture.
            captured = brain.capture_session_id("2026-05-01T10:00:00Z")
            self.assertIsNone(captured)

    def test_pre_existing_sessions_are_ignored(self):
        with _CodexHome() as home:
            for i in range(5):
                home.write_session(
                    f"rollout-2026-04-{i:02d}T10-{i:08d}-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
                )
            brain = CodexBrain(Path.cwd())
            brain._pre_state = brain.pre_invoke_snapshot()

            new_uuid = "abcdefab-cdef-abcd-efab-cdefabcdefab"
            home.write_session(f"rollout-2026-05-01T10-{new_uuid}.jsonl")

            captured = brain.capture_session_id("2026-05-01T10:00:00Z")
            self.assertEqual(captured, new_uuid)

    def test_missing_sessions_dir_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                brain = CodexBrain(Path.cwd())
                brain._pre_state = brain.pre_invoke_snapshot()
                self.assertIsNone(brain.capture_session_id("2026-05-01T10:00:00Z"))


if __name__ == "__main__":
    unittest.main()
