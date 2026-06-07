"""§18.1 operator compaction notification — body + gating."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.lifecycle import notify  # noqa: E402


class _Cfg:
    def __init__(self, *, enabled=True, chat_ids=("28547271",)):
        self.compaction_notify = type("C", (), {"enabled": enabled})()
        tg = type("T", (), {"chat_ids": tuple(chat_ids)})()
        self.channels = {"telegram": tg}


class _Runtime:
    def __init__(self, cfg):
        self.config = cfg
        self.instance_dir = Path("/tmp/does-not-matter")
        self.logs: list[tuple[str, str]] = []

    def log(self, msg, *, kind="", **_):
        self.logs.append((kind, msg))


class BodyTest(unittest.TestCase):
    def test_single_slot_body(self) -> None:
        body = notify.build_notification_body(
            trigger=notify.TRIGGER_COMPACT,
            channel="telegram",
            conversation_id="28547271",
            slots=[notify.SlotCompaction("claude", 0, 182_000, 0)],
        )
        self.assertIn("🧹 Context compacted (/compact)", body)
        self.assertIn("claude slot 0", body)
        self.assertIn("182K → 0 tokens", body)

    def test_multi_slot_batches(self) -> None:
        body = notify.build_notification_body(
            trigger=notify.TRIGGER_IDLE,
            channel="telegram",
            conversation_id="c",
            slots=[
                notify.SlotCompaction("claude", 0, 150_000, 0),
                notify.SlotCompaction("codex", 1, 90_000, 0),
            ],
            conversation_label="Luca DM",
        )
        self.assertIn("Luca DM", body)
        self.assertIn("claude slot 0:", body)
        self.assertIn("codex slot 1:", body)


class GatingTest(unittest.TestCase):
    def test_disabled_does_not_send(self) -> None:
        rt = _Runtime(_Cfg(enabled=False))
        sent = notify.notify_compaction(
            rt,
            trigger=notify.TRIGGER_COMPACT,
            channel="telegram",
            conversation_id="c",
            slots=[notify.SlotCompaction("claude", 0, 1000, 0)],
        )
        self.assertFalse(sent)

    def test_no_chat_id_does_not_send(self) -> None:
        rt = _Runtime(_Cfg(chat_ids=()))
        sent = notify.notify_compaction(
            rt,
            trigger=notify.TRIGGER_COMPACT,
            channel="telegram",
            conversation_id="c",
            slots=[notify.SlotCompaction("claude", 0, 1000, 0)],
        )
        self.assertFalse(sent)

    def test_enabled_sends_via_channel(self) -> None:
        rt = _Runtime(_Cfg(enabled=True))
        chan = mock.Mock()
        chan.ready.return_value = True
        with mock.patch("gateway.channels.telegram.TelegramChannel", return_value=chan):
            sent = notify.notify_compaction(
                rt,
                trigger=notify.TRIGGER_COMPACT,
                channel="telegram",
                conversation_id="28547271",
                slots=[notify.SlotCompaction("claude", 0, 182_000, 0)],
            )
        self.assertTrue(sent)
        body, target = chan.send.call_args.args
        self.assertEqual(target["chat_id"], "28547271")
        self.assertIn("182K → 0 tokens", body)

    def test_send_failure_logged_not_raised(self) -> None:
        rt = _Runtime(_Cfg(enabled=True))
        chan = mock.Mock()
        chan.ready.return_value = True
        chan.send.side_effect = RuntimeError("boom")
        with mock.patch("gateway.channels.telegram.TelegramChannel", return_value=chan):
            sent = notify.notify_compaction(
                rt,
                trigger=notify.TRIGGER_COMPACT,
                channel="telegram",
                conversation_id="c",
                slots=[notify.SlotCompaction("claude", 0, 1000, 0)],
            )
        self.assertFalse(sent)
        self.assertTrue(
            any(k == "context_compaction_notify_failed" for k, _ in rt.logs)
        )


if __name__ == "__main__":
    unittest.main()
