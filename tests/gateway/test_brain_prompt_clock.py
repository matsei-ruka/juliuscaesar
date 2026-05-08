"""Tests that brain prompts carry the configured clock block.

Covers docs/specs/timezone-config.md §Runtime injection:

- A non-Claude brain (preamble path) prepends the full clock block at the
  top of the rendered prompt.
- ClaudeBrain (auto-loads CLAUDE.md) does NOT prepend the full clock block;
  instead it inlines a single `[Current time: …]` line into the user
  message body.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import config as gateway_config  # noqa: E402
from gateway import context  # noqa: E402
from gateway.brains.claude import ClaudeBrain  # noqa: E402
from gateway.brains.opencode import OpencodeBrain  # noqa: E402
from gateway.queue import Event  # noqa: E402


def _write_instance(tmp: str, tz: str) -> Path:
    instance = Path(tmp)
    (instance / "ops").mkdir(parents=True, exist_ok=True)
    (instance / "ops" / "gateway.yaml").write_text(
        f"timezone: {tz}\ndefault_brain: claude\n"
    )
    (instance / "memory" / "L1").mkdir(parents=True, exist_ok=True)
    (instance / "memory" / "L1" / "IDENTITY.md").write_text("identity body")
    gateway_config.clear_config_cache()
    context.clear_cache()
    return instance


def _make_event(content: str = "hi") -> Event:
    return Event(
        id=1,
        source="telegram",
        source_message_id=None,
        user_id="u1",
        conversation_id="c1",
        content=content,
        meta="{}",
        status="pending",
        received_at="2026-05-08T10:00:00Z",
        available_at="2026-05-08T10:00:00Z",
        locked_by=None,
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


class NonClaudeBrainPromptTests(unittest.TestCase):
    def test_clock_block_prepended(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _write_instance(tmp, "Asia/Dubai")
            brain = OpencodeBrain(instance)
            text = brain.prompt_for_event(_make_event())
            self.assertTrue(text.lstrip().startswith("# Current time"))
            self.assertIn("Asia/Dubai", text)
            self.assertIn("UTC+04:00", text)

    def test_clock_block_falls_back_to_utc_on_missing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "memory" / "L1").mkdir(parents=True, exist_ok=True)
            (instance / "memory" / "L1" / "IDENTITY.md").write_text("identity")
            gateway_config.clear_config_cache()
            context.clear_cache()
            brain = OpencodeBrain(instance)
            text = brain.prompt_for_event(_make_event())
            self.assertIn("UTC", text)


class ClaudeBrainPromptTests(unittest.TestCase):
    def test_inline_clock_in_user_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _write_instance(tmp, "Asia/Dubai")
            brain = ClaudeBrain(instance)
            text = brain.prompt_for_event(_make_event(content="please reply"))
            # The full block should NOT be at the top — claude has no preamble.
            self.assertFalse(text.lstrip().startswith("# Current time"))
            # Inline form should be inside the user-message section.
            user_idx = text.index("# User message")
            tail = text[user_idx:]
            self.assertIn("[Current time:", tail)
            self.assertIn("Asia/Dubai", tail)
            self.assertIn("please reply", tail)


if __name__ == "__main__":
    unittest.main()
