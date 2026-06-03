"""Gateway brain adapter exports ORIGIN_CHAT_ID into the adapter subprocess env.

Per docs/specs/origin-chat-id.md:
  - event.meta["chat_id"] present → env["ORIGIN_CHAT_ID"] == that value
  - event.meta has no chat_id → env["ORIGIN_CHAT_ID"] is popped (so any
    leak from the parent process / a previous invocation does not bleed
    across events).

Drives a fake adapter that dumps `${ORIGIN_CHAT_ID:-__UNSET__}` to disk.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.brains.base import Brain  # noqa: E402
from gateway.config import BrainOverrideConfig  # noqa: E402
from gateway.queue import Event  # noqa: E402


def _event(*, meta: dict | None) -> Event:
    return Event(
        id=1,
        source="telegram",
        source_message_id="msg-1",
        user_id="u1",
        conversation_id="conv-1",
        content="hello",
        meta=json.dumps(meta) if meta is not None else None,
        status="running",
        received_at="2026-06-03T00:00:00Z",
        available_at="2026-06-03T00:00:00Z",
        locked_by="w",
        locked_until=None,
        started_at="2026-06-03T00:00:00Z",
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


class _FakeBrain(Brain):
    name = "fakeorigin"
    needs_l1_preamble = False


class OriginChatIdExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_ctx = tempfile.TemporaryDirectory()
        self.inst = Path(self.tmp_ctx.name)
        self.origin_out = self.inst / "origin.txt"
        adapter = self.inst / "fake.sh"
        adapter.write_text(
            "#!/usr/bin/env bash\n"
            'cat > /dev/null\n'
            f'printf "%s" "${{ORIGIN_CHAT_ID:-__UNSET__}}" > "{self.origin_out}"\n'
            'printf \'{"push_message_sent": false, "message": "ok"}\'\n'
        )
        adapter.chmod(0o755)
        self.adapter = adapter

    def tearDown(self) -> None:
        self.tmp_ctx.cleanup()

    def _invoke(self, *, meta: dict | None, parent_env: dict | None = None) -> str:
        brain = _FakeBrain(
            self.inst, override=BrainOverrideConfig(bin=str(self.adapter))
        )
        env_patch = parent_env or {}
        with mock.patch.dict(os.environ, env_patch, clear=False):
            brain.invoke(
                event=_event(meta=meta),
                model=None,
                resume_session=None,
                timeout_seconds=30,
                log_path=self.inst / "adapter.log",
            )
        return self.origin_out.read_text() if self.origin_out.exists() else ""

    def test_chat_id_in_meta_exports_origin_chat_id(self):
        captured = self._invoke(meta={"chat_id": "-1009999"})
        self.assertEqual(captured, "-1009999")

    def test_chat_id_coerced_to_string(self):
        # Telegram chat_ids arrive as ints in some channels.
        captured = self._invoke(meta={"chat_id": 28547271})
        self.assertEqual(captured, "28547271")

    def test_missing_chat_id_pops_parent_env_leak(self):
        captured = self._invoke(
            meta={"other": "value"},
            parent_env={"ORIGIN_CHAT_ID": "should-be-popped"},
        )
        self.assertEqual(captured, "__UNSET__")

    def test_null_meta_pops_parent_env_leak(self):
        captured = self._invoke(
            meta=None, parent_env={"ORIGIN_CHAT_ID": "leak"}
        )
        self.assertEqual(captured, "__UNSET__")


if __name__ == "__main__":
    unittest.main()
