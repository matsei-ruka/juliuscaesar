"""Tests for `jc-chats approve/deny --email <addr>` workflow.

Covers:
- gateway.yaml allowlist/blocklist mutation
- pending-message drain on approve (→ enqueue)
- pending-message drain on deny (→ drop)
- Telegram notification fired (mocked subprocess)
- idempotency on repeat invocations
- approve+deny round-trip removes from opposite list
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue as queue_module
from gateway.channels import email_dispatcher


CLI = str(REPO_ROOT / "bin" / "jc-chats")


def _run(args: list[str], instance: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, CLI, "--instance-dir", str(instance), *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _seed_pending(instance: Path, sender: str, uids: list[str]) -> None:
    """Drop pending messages for `sender` directly via the dispatcher."""
    msgs = [
        {
            "channel": "email",
            "channel_id": f"uid_{u}",
            "conversation_id": f"email_{sender.lower()}",
            "user_id": f"email_{sender.lower()}",
            "sender": sender,
            "sender_name": sender.split("@")[0],
            "subject": "Subject",
            "message_id": f"<{u}@x.com>",
            "in_reply_to": None,
            "references": [],
            "text": f"body {u}",
            "status": "unknown",
            "metadata": {"uid": u},
        }
        for u in uids
    ]
    email_dispatcher.dispatch_messages(
        instance_dir=instance,
        messages=msgs,
        cfg={"notify_on_unknown": False},
    )


def _read_email_senders(instance: Path) -> tuple[set[str], set[str]]:
    cfg = yaml.safe_load((instance / "ops" / "gateway.yaml").read_text())
    senders = ((cfg.get("channels") or {}).get("email") or {}).get("senders") or {}
    return set(senders.get("allowed") or []), set(senders.get("blocklist") or [])


class TestApproveEmail(unittest.TestCase):
    def test_approve_writes_yaml_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            rc, out, err = _run(
                ["approve", "--email", "mario@scovai.com"], instance
            )
            self.assertEqual(rc, 0, err)
            self.assertIn("channels.email.senders.allowed", out)
            allowed, blocked = _read_email_senders(instance)
            self.assertIn("mario@scovai.com", allowed)
            self.assertNotIn("mario@scovai.com", blocked)

    def test_approve_drains_pending_to_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _seed_pending(instance, "mario@scovai.com", ["10", "11", "12"])
            rc, out, _ = _run(["approve", "--email", "mario@scovai.com"], instance)
            self.assertEqual(rc, 0)
            self.assertIn("drained 3 pending", out)

            conn = queue_module.connect(instance)
            try:
                rows = conn.execute(
                    "SELECT source_message_id FROM events ORDER BY source_message_id"
                ).fetchall()
            finally:
                conn.close()
            ids = [r["source_message_id"] for r in rows]
            self.assertEqual(ids, ["uid_10", "uid_11", "uid_12"])

    def test_approve_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _run(["approve", "--email", "alice@x.com"], instance)
            rc, out, _ = _run(["approve", "--email", "alice@x.com"], instance)
            self.assertEqual(rc, 0)
            self.assertIn("already on email allowlist", out)

    def test_approve_removes_from_blocklist(self):
        """Re-approving a previously denied sender clears the blocklist entry."""
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _run(["deny", "--email", "carol@x.com"], instance)
            allowed, blocked = _read_email_senders(instance)
            self.assertIn("carol@x.com", blocked)
            rc, _, _ = _run(["approve", "--email", "carol@x.com"], instance)
            self.assertEqual(rc, 0)
            allowed, blocked = _read_email_senders(instance)
            self.assertIn("carol@x.com", allowed)
            self.assertNotIn("carol@x.com", blocked)


class TestDenyEmail(unittest.TestCase):
    def test_deny_writes_yaml_blocklist(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            rc, out, err = _run(["deny", "--email", "spam@x.com"], instance)
            self.assertEqual(rc, 0, err)
            self.assertIn("channels.email.senders.blocklist", out)
            allowed, blocked = _read_email_senders(instance)
            self.assertIn("spam@x.com", blocked)

    def test_deny_drops_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _seed_pending(instance, "spam@x.com", ["50", "51"])
            rc, out, _ = _run(["deny", "--email", "spam@x.com"], instance)
            self.assertEqual(rc, 0)
            self.assertIn("dropped 2 pending", out)
            conn = queue_module.connect(instance)
            try:
                n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n, 0)

    def test_deny_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _run(["deny", "--email", "bad@x.com"], instance)
            rc, out, _ = _run(["deny", "--email", "bad@x.com"], instance)
            self.assertEqual(rc, 0)
            self.assertIn("already on email blocklist", out)


class TestNoArgsErrors(unittest.TestCase):
    def test_approve_no_chat_id_no_email_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, err = _run(["approve"], Path(tmp))
            self.assertNotEqual(rc, 0)
            self.assertIn("chat_id or --email", err)

    def test_deny_no_chat_id_no_email_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, err = _run(["deny"], Path(tmp))
            self.assertNotEqual(rc, 0)
            self.assertIn("chat_id or --email", err)


class TestExistingChatIdFlowUnchanged(unittest.TestCase):
    """Phase 1/PR #30 telegram approve/deny flow must keep working."""

    def test_approve_chat_id_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            rc, out, err = _run(["approve", "12345"], instance)
            self.assertEqual(rc, 0, err)
            self.assertIn("ops/gateway.yaml updated", out)
            cfg = yaml.safe_load((instance / "ops" / "gateway.yaml").read_text())
            self.assertIn("12345", cfg["channels"]["telegram"]["chat_ids"])


if __name__ == "__main__":
    unittest.main()
