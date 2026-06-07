"""§18 + §18.1 — `/compact` end-to-end through the runtime.

Verifies the slash command rotates the conversation's active slots and emits a
single operator Telegram notification through the real channel send path
(mocked at the channel boundary, never hitting the live bot).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue, sessions  # noqa: E402
from gateway.config import render_default_config  # noqa: E402
from gateway.lifecycle import compaction, telemetry  # noqa: E402
from gateway.runtime import GatewayRuntime  # noqa: E402


def _config(*, notify_enabled: bool = True) -> str:
    text = render_default_config(default_brain="claude:sonnet-4-6")
    text = text.replace("chat_ids: []", 'chat_ids: ["28547271"]')
    if not notify_enabled:
        text = text.replace(
            "compaction_notify:\n  enabled: true",
            "compaction_notify:\n  enabled: false",
        )
    return text


def _make_instance(*, notify_enabled: bool = True) -> Path:
    root = Path(tempfile.mkdtemp(prefix="jc-compact-runtime-"))
    (root / ".jc").write_text("", encoding="utf-8")
    (root / "ops").mkdir()
    (root / "memory" / "L1").mkdir(parents=True)
    (root / "memory" / "L1" / "IDENTITY.md").write_text("Test", encoding="utf-8")
    (root / "ops" / "gateway.yaml").write_text(_config(notify_enabled=notify_enabled), encoding="utf-8")
    return root


def _seed(instance: Path) -> None:
    conn = queue.connect(instance)
    try:
        sessions.upsert_session(
            conn,
            channel="telegram",
            conversation_id="28547271",
            brain="claude",
            session_id="sess-claude-aaaa",
            slot=0,
        )
        telemetry.record_usage(
            conn,
            owner_key=compaction.owner_key("telegram", "28547271", "claude", 0),
            brain="claude",
            usage=telemetry.ContextUsage.from_anthropic_usage({"input_tokens": 120_000}),
        )
        queue.enqueue(
            conn,
            source="telegram",
            source_message_id="m-compact",
            conversation_id="28547271",
            content="/compact",
            meta={"chat_id": "28547271"},
        )
    finally:
        conn.close()


class CompactionNotifyRuntimeTest(unittest.TestCase):
    def test_compact_rotates_and_notifies_operator(self) -> None:
        instance = _make_instance(notify_enabled=True)
        _seed(instance)
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        chan = mock.Mock()
        chan.ready.return_value = True
        with mock.patch(
            "gateway.channels.telegram.TelegramChannel", return_value=chan
        ), mock.patch("gateway.runtime.deliver_response", return_value="m-out") as deliver:
            self.assertTrue(runtime.dispatch_once())

        # Operator notification fired once, through the channel send path.
        chan.send.assert_called_once()
        body, target = chan.send.call_args.args
        self.assertEqual(target["chat_id"], "28547271")
        self.assertIn("🧹 Context compacted (/compact)", body)
        self.assertIn("claude slot 0", body)

        # The user-facing report was delivered.
        self.assertIn("Context maintenance complete", deliver.call_args.kwargs["response"])

        # Session mapping cleared by rotation.
        conn = queue.connect(instance)
        try:
            self.assertIsNone(
                sessions.get_session(
                    conn, channel="telegram", conversation_id="28547271", brain="claude", slot=0
                )
            )
            tel = telemetry.get_telemetry(
                conn,
                owner_key=compaction.owner_key("telegram", "28547271", "claude", 0),
            )
            assert tel is not None
            self.assertIsNone(tel.effective_input_tokens)
        finally:
            conn.close()
        runtime.close()

    def test_compact_notify_disabled_skips_operator_message(self) -> None:
        instance = _make_instance(notify_enabled=False)
        _seed(instance)
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        chan = mock.Mock()
        chan.ready.return_value = True
        with mock.patch(
            "gateway.channels.telegram.TelegramChannel", return_value=chan
        ), mock.patch("gateway.runtime.deliver_response", return_value="m-out"):
            self.assertTrue(runtime.dispatch_once())
        chan.send.assert_not_called()
        runtime.close()


if __name__ == "__main__":
    unittest.main()
