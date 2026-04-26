"""`prompt_for_event` injects the Known Telegram chats section."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import chats  # noqa: E402
from gateway.brains.base import Brain  # noqa: E402
from gateway.queue import Event  # noqa: E402


def _make_event(source: str = "telegram") -> Event:
    return Event(
        id=1,
        source=source,
        source_message_id="42",
        user_id="u",
        conversation_id="c",
        content="hello",
        meta=None,
        status="running",
        received_at="2026-04-26T00:00:00Z",
        available_at="2026-04-26T00:00:00Z",
        locked_by=None,
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


def _stub_instance(tmp: str) -> Path:
    instance = Path(tmp)
    (instance / "memory" / "L1").mkdir(parents=True)
    (instance / "memory" / "L1" / "IDENTITY.md").write_text("Julius", encoding="utf-8")
    return instance


class ChatPreambleTests(unittest.TestCase):
    def test_telegram_event_includes_known_chats_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _stub_instance(tmp)
            chats.upsert_chat(
                instance,
                channel="telegram",
                chat_id="28547271",
                chat_type="private",
                title="Luca Mattei",
                username="luca",
                last_message_id="100",
            )
            chats.upsert_chat(
                instance,
                channel="telegram",
                chat_id="-100",
                chat_type="supergroup",
                title="BNESIM ops",
                member_count=8,
                last_message_id="200",
            )
            chats.upsert_chat(
                instance,
                channel="telegram",
                chat_id="-200",
                chat_type="group",
                title="Cardcentric",
                member_count=4,
                last_message_id="300",
            )
            brain = Brain(instance)
            brain.name = "test"
            prompt = brain.prompt_for_event(_make_event())
            self.assertIn("## Known Telegram chats", prompt)
            self.assertIn("28547271", prompt)
            self.assertIn("Luca Mattei", prompt)
            self.assertIn("BNESIM ops", prompt)
            self.assertIn("Cardcentric", prompt)
            # Section must come before the event block.
            self.assertLess(
                prompt.index("## Known Telegram chats"),
                prompt.index("# Incoming event"),
            )

    def test_section_absent_when_no_chats(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _stub_instance(tmp)
            brain = Brain(instance)
            brain.name = "test"
            prompt = brain.prompt_for_event(_make_event())
            self.assertNotIn("## Known Telegram chats", prompt)

    def test_section_absent_for_non_telegram_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _stub_instance(tmp)
            chats.upsert_chat(
                instance,
                channel="telegram",
                chat_id="1",
                title="Luca",
            )
            brain = Brain(instance)
            brain.name = "test"
            prompt = brain.prompt_for_event(_make_event(source="cron"))
            self.assertNotIn("## Known Telegram chats", prompt)

    def test_section_absent_when_brain_skips_l1_preamble(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _stub_instance(tmp)
            chats.upsert_chat(
                instance, channel="telegram", chat_id="1", title="Luca"
            )
            brain = Brain(instance)
            brain.name = "test"
            brain.needs_l1_preamble = False
            prompt = brain.prompt_for_event(_make_event())
            self.assertNotIn("## Known Telegram chats", prompt)


if __name__ == "__main__":
    unittest.main()
