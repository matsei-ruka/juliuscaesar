"""Tests for the email dispatcher: routing logic + pending drain."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway.channels import email_dispatcher
from gateway import queue as queue_module


def _make_msg(uid: str, sender: str, status: str, **overrides):
    base = {
        "channel": "email",
        "channel_id": f"uid_{uid}",
        "conversation_id": f"email_{sender.lower()}",
        "user_id": f"email_{sender.lower()}",
        "sender": sender,
        "sender_name": sender.split("@")[0],
        "subject": "Hi",
        "message_id": f"<{uid}@scovai.com>",
        "in_reply_to": None,
        "references": [],
        "text": f"[EMAIL from {sender}]\n\nbody {uid}",
        "status": status,
        "metadata": {"uid": uid, "date": "2026-04-30T10:00:00", "is_unread": True},
    }
    base.update(overrides)
    return base


class TestDispatchAllowed(unittest.TestCase):
    def test_trusted_messages_get_enqueued(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            messages = [
                _make_msg("100", "mario@scovai.com", "trusted"),
                _make_msg("101", "filippo@scovai.com", "trusted"),
            ]
            result = email_dispatcher.dispatch_messages(
                instance_dir=instance, messages=messages, cfg={"notify_on_unknown": False}
            )
            self.assertEqual(result.dispatched, 2)
            self.assertEqual(result.pending, 0)
            self.assertEqual(result.blocked, 0)

            conn = queue_module.connect(instance)
            try:
                rows = conn.execute(
                    "SELECT source, source_message_id, content, meta FROM events ORDER BY id"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["source"], "email")
            self.assertEqual(rows[0]["source_message_id"], "uid_100")
            meta = json.loads(rows[0]["meta"])
            self.assertEqual(meta["delivery_channel"], "email")
            self.assertEqual(meta["email_to"], "mario@scovai.com")
            self.assertEqual(meta["sender_tier"], "trusted")
            self.assertIn("body 100", rows[0]["content"])

    def test_allowed_uses_injected_enqueue(self):
        captured = []

        def fake_enqueue(**kwargs):
            captured.append(kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            email_dispatcher.dispatch_messages(
                instance_dir=instance,
                messages=[_make_msg("1", "mario@scovai.com", "trusted")],
                enqueue=fake_enqueue,
            )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["source"], "email")
        self.assertEqual(captured[0]["meta"]["delivery_channel"], "email")
        self.assertEqual(captured[0]["meta"]["sender_tier"], "trusted")

    def test_external_messages_get_enqueued_with_external_tier(self):
        captured = []

        def fake_enqueue(**kwargs):
            captured.append(kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            result = email_dispatcher.dispatch_messages(
                instance_dir=instance,
                messages=[_make_msg("2", "client@example.com", "external")],
                enqueue=fake_enqueue,
            )
        self.assertEqual(result.dispatched, 1)
        self.assertEqual(result.handled_uids, ["2"])
        self.assertEqual(captured[0]["meta"]["sender_tier"], "external")


class TestDispatchBlocked(unittest.TestCase):
    def test_blocked_silently_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            with patch.object(email_dispatcher, "_send_telegram_notify") as notify:
                result = email_dispatcher.dispatch_messages(
                    instance_dir=instance,
                    messages=[_make_msg("200", "spam@x.com", "blocked")],
                )
            self.assertEqual(result.blocked, 1)
            self.assertEqual(result.dispatched, 0)
            self.assertEqual(result.pending, 0)
            notify.assert_not_called()
            # No pending dir written.
            self.assertFalse(email_dispatcher.pending_dir(instance).exists())


class TestDispatchUnknown(unittest.TestCase):
    def test_unknown_persists_pending_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            with patch.object(
                email_dispatcher, "_send_telegram_notify", return_value="42"
            ) as notify:
                result = email_dispatcher.dispatch_messages(
                    instance_dir=instance,
                    messages=[_make_msg("300", "newguy@corp.com", "unknown")],
                    cfg={"notify_on_unknown": True, "telegram_chat_id": "999"},
                )
            self.assertEqual(result.pending, 1)
            notify.assert_called_once()
            args, kwargs = notify.call_args
            self.assertEqual(kwargs.get("chat_id_override"), "999")
            self.assertIn("newguy@corp.com", args[1])

            pdir = email_dispatcher.pending_dir(instance) / email_dispatcher._sender_key(
                "newguy@corp.com"
            )
            self.assertTrue(pdir.is_dir())
            files = list(pdir.glob("*.json"))
            self.assertEqual(len(files), 1)
            saved = json.loads(files[0].read_text())
            self.assertEqual(saved["sender"], "newguy@corp.com")

    def test_unknown_skips_notification_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            with patch.object(email_dispatcher, "_send_telegram_notify") as notify:
                email_dispatcher.dispatch_messages(
                    instance_dir=instance,
                    messages=[_make_msg("301", "newguy@corp.com", "unknown")],
                    cfg={"notify_on_unknown": False},
                )
            notify.assert_not_called()

    def test_unknown_sender_uses_safe_path_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            sender = 'weird/name+tag"@corp.com'
            email_dispatcher.dispatch_messages(
                instance_dir=instance,
                messages=[_make_msg("302", sender, "unknown")],
                cfg={"notify_on_unknown": False},
            )
            raw_path = email_dispatcher.pending_dir(instance) / sender
            safe_path = email_dispatcher.pending_dir(instance) / email_dispatcher._sender_key(
                sender
            )
            self.assertFalse(raw_path.exists())
            self.assertTrue(safe_path.is_dir())


class TestDrainPending(unittest.TestCase):
    def test_drain_approve_enqueues_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            email_dispatcher.dispatch_messages(
                instance_dir=instance,
                messages=[
                    _make_msg("400", "alice@x.com", "unknown"),
                    _make_msg("401", "alice@x.com", "unknown"),
                ],
                cfg={"notify_on_unknown": False},
            )
            count = email_dispatcher.drain_pending(instance, "alice@x.com", action="approve")
            self.assertEqual(count, 2)

            conn = queue_module.connect(instance)
            try:
                n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n, 2)
            # Pending dir cleared.
            sender_dir = email_dispatcher.pending_dir(instance) / email_dispatcher._sender_key(
                "alice@x.com"
            )
            self.assertFalse(sender_dir.exists())

    def test_drain_deny_drops_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            email_dispatcher.dispatch_messages(
                instance_dir=instance,
                messages=[_make_msg("500", "bad@x.com", "unknown")],
                cfg={"notify_on_unknown": False},
            )
            count = email_dispatcher.drain_pending(instance, "bad@x.com", action="deny")
            self.assertEqual(count, 1)
            conn = queue_module.connect(instance)
            try:
                n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n, 0)

    def test_drain_no_pending_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            count = email_dispatcher.drain_pending(instance, "ghost@x.com", action="approve")
            self.assertEqual(count, 0)


class TestMetaShape(unittest.TestCase):
    def test_meta_carries_outbound_fields(self):
        msg = _make_msg(
            "9",
            "mario@scovai.com",
            "trusted",
            references=["<root@x.com>", "<prev@x.com>"],
            in_reply_to="<prev@x.com>",
        )
        meta = email_dispatcher._meta_for_event(msg)
        self.assertEqual(meta["delivery_channel"], "email")
        self.assertEqual(meta["email_to"], "mario@scovai.com")
        self.assertEqual(meta["email_subject"], "Hi")
        self.assertEqual(meta["email_message_id"], "<9@scovai.com>")
        self.assertEqual(meta["email_in_reply_to"], "<prev@x.com>")
        self.assertEqual(meta["email_references"], ["<root@x.com>", "<prev@x.com>"])
        self.assertEqual(meta["sender_tier"], "trusted")


class TestDrafts(unittest.TestCase):
    def test_enqueue_draft_persists_safe_sender_path_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            meta = {
                "email_to": 'client/name"@example.com',
                "email_subject": "Status",
                "email_message_id": "<1@example.com>",
                "email_uid": "777",
                "sender_tier": "external",
            }
            with patch.object(email_dispatcher, "_send_draft_with_buttons") as notify:
                draft_id = email_dispatcher.enqueue_draft(
                    instance,
                    response="Draft body",
                    meta=meta,
                    cfg={"approvals": {"telegram_chat_id": "123"}},
                )
            self.assertEqual(draft_id, "draft_777")
            raw_path = email_dispatcher.drafts_dir(instance) / meta["email_to"]
            safe_path = (
                email_dispatcher.drafts_dir(instance)
                / email_dispatcher._sender_key(meta["email_to"])
                / "draft_777.json"
            )
            self.assertFalse(raw_path.exists())
            self.assertTrue(safe_path.exists())
            saved = json.loads(safe_path.read_text())
            self.assertEqual(saved["sender"], meta["email_to"])
            self.assertEqual(saved["state"], "pending")
            notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
