"""Tests for slot-aware rendering in `supervisor.cards.render_card`.

Acceptance for parallel-slots backward compatibility (docs/specs/parallel-slots.md):

- When `slot is None` OR `max_concurrent <= 1` the renderer output must be
  byte-identical to the pre-parallel-slots version (phase emoji as leading
  card prefix, no slot indicator).
- When `slot is not None AND max_concurrent > 1` the leading character is
  the slot keycap (0️⃣..9️⃣) and the phase emoji moves to line 2.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from supervisor.cards import _slot_emoji, render_card  # noqa: E402
from supervisor.models import PhaseResult  # noqa: E402


def _phase() -> PhaseResult:
    return PhaseResult(phase="coding", emoji="🛠️", label={"en": "coding", "it": "sviluppo"})


class SlotEmojiTests(unittest.TestCase):
    def test_slot_emoji_maps_0_through_9(self) -> None:
        expected = ("0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣")
        for n, want in enumerate(expected):
            self.assertEqual(_slot_emoji(n), want)

    def test_slot_emoji_clamps_above_nine(self) -> None:
        self.assertEqual(_slot_emoji(15), "9️⃣")

    def test_slot_emoji_clamps_negative(self) -> None:
        self.assertEqual(_slot_emoji(-3), "0️⃣")


class RenderCardSerialBackwardCompatTests(unittest.TestCase):
    """N=1 path must reproduce the pre-parallel-slots renderer exactly."""

    def test_render_without_slot_kwarg_matches_legacy(self) -> None:
        card = render_card(
            title="audit Athena repo",
            phase=_phase(),
            elapsed_seconds=125.0,
        )
        self.assertTrue(card.text.startswith("🛠️ audit Athena repo"))
        self.assertNotIn("0️⃣", card.text)
        self.assertNotIn("1️⃣", card.text)

    def test_render_with_slot_but_max_concurrent_one_matches_legacy(self) -> None:
        legacy = render_card(
            title="x",
            phase=_phase(),
            elapsed_seconds=10.0,
        )
        with_slot_n1 = render_card(
            title="x",
            phase=_phase(),
            elapsed_seconds=10.0,
            slot=2,
            max_concurrent=1,
        )
        self.assertEqual(legacy.text, with_slot_n1.text)


class RenderCardParallelTests(unittest.TestCase):
    """N>1 path swaps the leading emoji for the slot keycap."""

    def test_slot_emoji_replaces_phase_emoji_prefix(self) -> None:
        card = render_card(
            title="audit Athena repo",
            phase=_phase(),
            elapsed_seconds=10.0,
            slot=1,
            max_concurrent=3,
        )
        first_line = card.text.splitlines()[0]
        self.assertTrue(first_line.startswith("1️⃣ "))
        self.assertNotIn("🛠️", first_line)

    def test_phase_emoji_moves_to_line_two(self) -> None:
        card = render_card(
            title="x",
            phase=_phase(),
            elapsed_seconds=130.0,
            slot=0,
            max_concurrent=2,
        )
        lines = card.text.splitlines()
        self.assertTrue(lines[0].startswith("0️⃣ "))
        # Line 1 is blank; phase emoji + minute bar on line 2.
        self.assertIn("🛠️", lines[2])

    def test_narration_appears_on_line_two_with_phase_emoji(self) -> None:
        card = render_card(
            title="x",
            phase=_phase(),
            elapsed_seconds=10.0,
            narration="found 184 files",
            slot=2,
            max_concurrent=3,
        )
        lines = card.text.splitlines()
        self.assertTrue(lines[0].startswith("2️⃣"))
        # Activity line carries narration alongside the phase emoji.
        self.assertIn("found 184 files", lines[2])
        self.assertIn("🛠️", lines[2])


if __name__ == "__main__":
    unittest.main()
