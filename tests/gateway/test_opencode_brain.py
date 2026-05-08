"""Tests for OpenCode gateway wrapper behavior."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.brains.opencode import OpencodeBrain  # noqa: E402


class OpencodeSessionCaptureTests(unittest.TestCase):
    def test_captures_session_with_numeric_created_timestamp(self):
        sessions = [
            {
                "id": "old",
                "created": 1778220000000,
                "directory": "/tmp/elsewhere",
            },
            {
                "id": "ses_new",
                "created": 1778226665499,
                "directory": "/tmp/instance",
            },
        ]
        proc = mock.Mock(returncode=0, stdout=json.dumps(sessions))
        with mock.patch("subprocess.run", return_value=proc) as run:
            captured = OpencodeBrain(Path("/tmp/instance")).capture_session_id(
                "2026-05-08T07:51:00Z"
            )

        self.assertEqual(captured, "ses_new")
        run.assert_called_once_with(
            ["opencode", "session", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd="/tmp/instance",
        )

    def test_ignores_sessions_before_adapter_start(self):
        proc = mock.Mock(
            returncode=0,
            stdout=json.dumps([{"id": "old", "created": 1778220000000}]),
        )
        with mock.patch("subprocess.run", return_value=proc):
            captured = OpencodeBrain(Path("/tmp/instance")).capture_session_id(
                "2026-05-08T07:51:00Z"
            )

        self.assertIsNone(captured)

    def test_ignores_sessions_from_other_directories(self):
        sessions = [
            {
                "id": "elsewhere",
                "created": 1778226665499,
                "directory": "/tmp/elsewhere",
            }
        ]
        proc = mock.Mock(returncode=0, stdout=json.dumps(sessions))
        with mock.patch("subprocess.run", return_value=proc):
            captured = OpencodeBrain(Path("/tmp/instance")).capture_session_id(
                "2026-05-08T07:51:00Z"
            )

        self.assertIsNone(captured)

    def test_uses_updated_timestamp_for_session_activity(self):
        sessions = [
            {
                "id": "ses_updated",
                "created": 1778220000000,
                "updated": 1778226665499,
                "directory": "/tmp/instance",
            }
        ]
        proc = mock.Mock(returncode=0, stdout=json.dumps(sessions))
        with mock.patch("subprocess.run", return_value=proc):
            captured = OpencodeBrain(Path("/tmp/instance")).capture_session_id(
                "2026-05-08T07:51:00Z"
            )

        self.assertEqual(captured, "ses_updated")


if __name__ == "__main__":
    unittest.main()
