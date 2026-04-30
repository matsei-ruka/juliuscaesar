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
    def test_allowed_messages_get_enqueued(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            messages = [
                _make_msg("100", "mario@scovai.com", "allowed"),
                _make_msg("101", "filippo@scovai.com", "allowed"),
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
            self.assertIn("body 100", rows[0]["content"])

    def test_allowed_uses_injected_enqueue(self):
        captured = []

        def fake_enqueue(**kwargs):
            captured.append(kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            email_dispatcher.dispatch_messages(
                instance_dir=instance,
                messages=[_make_msg("1", "mario@scovai.com", "allowed")],
                enqueue=fake_enqueue,
            )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["source"], "email")
        self.assertEqual(captured[0]["meta"]["delivery_channel"], "email")


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

            pdir = email_dispatcher.pending_dir(instance) / "newguy@corp.com"
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
            sender_dir = email_dispatcher.pending_dir(instance) / "alice@x.com"
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
            "allowed",
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


if __name__ == "__main__":
    unittest.main()
