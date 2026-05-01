"""Tests for the `jc email` operator CLI."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue as queue_module  # noqa: E402
from gateway.channels import email_dispatcher, email_state  # noqa: E402


CLI = REPO_ROOT / "bin" / "jc-email"


def _run(args: list[str], instance: Path, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), "--instance-dir", str(instance), *args],
        input=stdin,
        capture_output=True,
        text=True,
    )


def _seed_pending(instance: Path, sender: str, uid: str = "10") -> None:
    email_dispatcher.dispatch_messages(
        instance_dir=instance,
        messages=[
            {
                "channel": "email",
                "channel_id": f"uid_{uid}",
                "conversation_id": f"email_{sender.lower()}",
                "user_id": f"email_{sender.lower()}",
                "sender": sender,
                "sender_name": sender.split("@")[0],
                "subject": "Question",
                "message_id": f"<{uid}@x.com>",
                "in_reply_to": None,
                "references": [],
                "text": f"body {uid}",
                "status": "unknown",
                "metadata": {"uid": uid, "date": "2026-05-01T10:00:00Z"},
            }
        ],
        cfg={"notify_on_unknown": False},
    )


def _seed_draft(instance: Path, sender: str = "client@example.com") -> str:
    with patch.object(email_dispatcher, "_send_telegram_notify"):
        return email_dispatcher.enqueue_draft(
            instance,
            response="Draft body",
            meta={
                "email_to": sender,
                "email_subject": "Status",
                "email_message_id": "<1@example.com>",
                "email_references": ["<root@example.com>"],
                "email_uid": "777",
                "sender_tier": "external",
            },
            cfg={"approvals": {"notify_on_draft": False}},
        )


def _load_cli_module():
    loader = SourceFileLoader("jc_email_cli", str(CLI))
    spec = importlib.util.spec_from_loader("jc_email_cli", loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EmailCliPendingTests(unittest.TestCase):
    def test_pending_list_show_and_approve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _seed_pending(instance, "new@example.com", uid="10")

            proc = _run(["pending", "list"], instance)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("new@example.com", proc.stdout)
            self.assertIn("Question", proc.stdout)

            proc = _run(["pending", "show", "10"], instance)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["sender"], "new@example.com")

            proc = _run(["pending", "approve", "new@example.com"], instance)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("drained=1", proc.stdout)

            conn = queue_module.connect(instance)
            try:
                count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 1)


class EmailCliDraftTests(unittest.TestCase):
    def test_drafts_list_show_edit_reject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            draft_id = _seed_draft(instance)

            proc = _run(["drafts", "list"], instance)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(draft_id, proc.stdout)
            self.assertIn("pending", proc.stdout)

            proc = _run(["drafts", "show", draft_id], instance)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["draft_text"], "Draft body")

            proc = _run(["drafts", "edit", draft_id], instance, stdin="Edited body")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("edit_count=1", proc.stdout)

            record = email_state.find_draft(instance, draft_id)
            self.assertIsNotNone(record)
            self.assertEqual(record.data["draft_text"], "Edited body")

            proc = _run(["drafts", "reject", draft_id], instance)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            record = email_state.find_draft(instance, draft_id)
            self.assertEqual(record.data["state"], "rejected")

    def test_draft_approve_marks_sent_with_message_id(self) -> None:
        module = _load_cli_module()

        class FakeAdapter:
            def send_reply(self, **_kwargs):
                return "<sent@example.com>"

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            draft_id = _seed_draft(instance)
            with patch.object(module, "_adapter", return_value=FakeAdapter()):
                rc = module.main(["--instance-dir", str(instance), "drafts", "approve", draft_id])
            self.assertEqual(rc, 0)
            record = email_state.find_draft(instance, draft_id)
            self.assertEqual(record.data["state"], "sent")
            self.assertEqual(record.data["sent_message_id"], "<sent@example.com>")


class EmailCliDoctorTests(unittest.TestCase):
    def test_doctor_reports_state_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "ops").mkdir()
            (instance / "ops" / "gateway.yaml").write_text(
                "channels:\n  email:\n    enabled: true\n",
                encoding="utf-8",
            )
            _seed_pending(instance, "new@example.com", uid="99")
            _seed_draft(instance)
            proc = _run(["doctor"], instance)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("pending:  1", proc.stdout)
            self.assertIn("drafts:   1", proc.stdout)
            self.assertIn("IMAP_HOST: missing", proc.stdout)
            self.assertIn("last_event: draft_queued", proc.stdout)

    def test_doctor_json_reports_metrics_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "ops").mkdir()
            (instance / "ops" / "gateway.yaml").write_text(
                "channels:\n  email:\n    enabled: true\n",
                encoding="utf-8",
            )
            _seed_pending(instance, "new@example.com", uid="100")
            proc = _run(["doctor", "--json"], instance)
            self.assertEqual(proc.returncode, 1)
            data = json.loads(proc.stdout)
            self.assertEqual(data["pending"], 1)
            self.assertEqual(data["event_counts_recent"]["inbound_pending"], 1)
            self.assertEqual(data["last_event"]["event"], "inbound_pending")


if __name__ == "__main__":
    unittest.main()
