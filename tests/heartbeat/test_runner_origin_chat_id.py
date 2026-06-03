"""Heartbeat runner exports ORIGIN_CHAT_ID to the adapter env.

Per docs/specs/origin-chat-id.md:
  - Single-destination task → ORIGIN_CHAT_ID set to that destination's chat_id.
  - Multi-destination (fanout) task → ORIGIN_CHAT_ID NOT set (left to per-send loop).
  - No destination → ORIGIN_CHAT_ID NOT set; the deprecated TELEGRAM_CHAT_ID path
    in send_telegram.py still resolves (with stderr warning).
  - A parent-shell ORIGIN_CHAT_ID is popped on no-destination runs to prevent leak.
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

from heartbeat import runner as runner_mod  # noqa: E402


class _StubProc:
    def __init__(self, env):
        self.captured_env = env
        self.returncode = 0

    def communicate(self, prompt, timeout=None):
        return "stub stdout", ""


def _make_instance(tmp: str, *, task_dest: str | list | None) -> Path:
    instance = Path(tmp)
    (instance / "heartbeat").mkdir(parents=True)
    (instance / "memory" / "L1").mkdir(parents=True)
    (instance / "memory" / "L1" / "CHATS.md").write_text("", encoding="utf-8")
    (instance / "ops").mkdir(parents=True)
    (instance / "ops" / "gateway.yaml").write_text("timezone: UTC\n")
    if task_dest is None:
        dest_line = ""
    elif isinstance(task_dest, str):
        dest_line = f"    destination: {task_dest}\n"
    else:
        dest_line = "    destination:\n" + "".join(f"      - {n}\n" for n in task_dest)
    (instance / "heartbeat" / "tasks.yaml").write_text(
        "destinations:\n"
        "  ops_group: { chat_id: '-1001', channel: telegram }\n"
        "  luca_dm:   { chat_id: '28547271', channel: telegram }\n"
        "tasks:\n"
        "  noop:\n"
        "    tool: claude\n"
        "    prompt: 'hello'\n"
        f"{dest_line}"
    )
    return instance


class OriginChatIdExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = self.tmp_ctx.name

    def tearDown(self) -> None:
        self.tmp_ctx.cleanup()

    def _run_capture_env(self, task_dest):
        instance = _make_instance(self.tmp, task_dest=task_dest)
        captured_env = {}

        def fake_popen(*args, env=None, **kwargs):
            captured_env.update(env or {})
            proc = _StubProc(captured_env)
            return proc

        with mock.patch.object(runner_mod.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(runner_mod, "parse_brain_output") as parse_mock, \
             mock.patch.object(runner_mod, "send_telegram", return_value=None), \
             mock.patch.object(runner_mod, "capture_session_id", return_value=None):
            parse_mock.return_value = mock.Mock(
                parse_error=None,
                push_message_sent=False,
                message="",  # empty → runner returns without sending
            )
            rc = runner_mod.run_task(instance, "noop", dry_run=False)
        return rc, captured_env

    def test_single_destination_exports_origin_chat_id(self):
        _, env = self._run_capture_env(task_dest="ops_group")
        self.assertEqual(env.get("ORIGIN_CHAT_ID"), "-1001")

    def test_multi_destination_fanout_does_not_export_origin_chat_id(self):
        _, env = self._run_capture_env(task_dest=["ops_group", "luca_dm"])
        self.assertNotIn("ORIGIN_CHAT_ID", env)

    def test_no_destination_does_not_export_origin_chat_id(self):
        _, env = self._run_capture_env(task_dest=None)
        self.assertNotIn("ORIGIN_CHAT_ID", env)

    def test_no_destination_pops_parent_shell_origin_chat_id(self):
        with mock.patch.dict(os.environ, {"ORIGIN_CHAT_ID": "leaked"}, clear=False):
            _, env = self._run_capture_env(task_dest=None)
        self.assertNotIn("ORIGIN_CHAT_ID", env)


if __name__ == "__main__":
    unittest.main()
