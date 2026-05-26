"""Tests for lib/gateway/goal_cache.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import goal_cache  # noqa: E402


def _backdate(instance_dir: Path, conversation_id: str, seconds: int) -> None:
    """Rewrite an entry's set_at to `seconds` in the past."""
    import json

    path = goal_cache._path(instance_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data[conversation_id]["set_at"] = ts
    path.write_text(json.dumps(data), encoding="utf-8")


class SetGetClearTests(unittest.TestCase):
    def test_set_get_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            self.assertTrue(goal_cache.set(inst, "task-root:R", "t1", "Do the thing"))
            entry = goal_cache.get(inst, "task-root:R")
            self.assertEqual(entry["task_id"], "t1")
            self.assertEqual(entry["text"], "Do the thing")
            self.assertEqual(goal_cache.goal_text(inst, "task-root:R"), "Do the thing")

    def test_get_absent_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            self.assertIsNone(goal_cache.get(inst, "task-root:nope"))
            self.assertEqual(goal_cache.goal_text(inst, "telegram:123"), "")

    def test_clear_matching_task_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            goal_cache.set(inst, "task-root:R", "t1", "x")
            self.assertTrue(goal_cache.clear(inst, "task-root:R", "t1"))
            self.assertIsNone(goal_cache.get(inst, "task-root:R"))

    def test_clear_mismatched_task_id_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            goal_cache.set(inst, "task-root:R", "t1", "x")
            # stale close for a different task → must preserve the active goal
            self.assertFalse(goal_cache.clear(inst, "task-root:R", "OLD"))
            self.assertEqual(goal_cache.goal_text(inst, "task-root:R"), "x")

    def test_clear_without_task_id_drops(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            goal_cache.set(inst, "task-root:R", "t1", "x")
            self.assertTrue(goal_cache.clear(inst, "task-root:R"))
            self.assertIsNone(goal_cache.get(inst, "task-root:R"))

    def test_set_replaces_on_new_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            goal_cache.set(inst, "task-root:R", "t1", "first")
            goal_cache.set(inst, "task-root:R", "t2", "second")
            entry = goal_cache.get(inst, "task-root:R")
            self.assertEqual(entry["task_id"], "t2")
            self.assertEqual(entry["text"], "second")

    def test_isolation_between_conversations(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            goal_cache.set(inst, "task-root:A", "ta", "goalA")
            goal_cache.set(inst, "task-root:B", "tb", "goalB")
            self.assertEqual(goal_cache.goal_text(inst, "task-root:A"), "goalA")
            self.assertEqual(goal_cache.goal_text(inst, "task-root:B"), "goalB")


class DurabilityTests(unittest.TestCase):
    def test_corrupt_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            p = goal_cache._path(inst)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{not json", encoding="utf-8")
            self.assertIsNone(goal_cache.get(inst, "task-root:R"))

    def test_atomic_write_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            goal_cache.set(inst, "task-root:R", "t1", "x")
            gateway_dir = goal_cache._path(inst).parent
            self.assertEqual(list(gateway_dir.glob("*.tmp")), [])


class TtlTests(unittest.TestCase):
    def test_expired_entry_treated_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            goal_cache.set(inst, "task-root:R", "t1", "x")
            _backdate(inst, "task-root:R", 7200)  # 2h old
            self.assertIsNone(goal_cache.get(inst, "task-root:R", ttl_seconds=3600))
            # but visible with TTL disabled
            self.assertIsNotNone(goal_cache.get(inst, "task-root:R", ttl_seconds=None))

    def test_get_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            goal_cache.set(inst, "task-root:R", "t1", "x")
            _backdate(inst, "task-root:R", 7200)
            goal_cache.get(inst, "task-root:R", ttl_seconds=3600)  # expired read
            # reader must not delete; sweep is the writer
            self.assertIn("task-root:R", goal_cache.all_goals(inst))

    def test_sweep_prunes_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            goal_cache.set(inst, "task-root:R", "t1", "x")
            goal_cache.set(inst, "task-root:fresh", "t2", "y")
            _backdate(inst, "task-root:R", 7200)
            removed = goal_cache.sweep(inst, ttl_seconds=3600)
            self.assertEqual(removed, 1)
            self.assertNotIn("task-root:R", goal_cache.all_goals(inst))
            self.assertIn("task-root:fresh", goal_cache.all_goals(inst))


class FormatGoalTests(unittest.TestCase):
    def test_title_and_description(self):
        text = goal_cache.format_goal({"title": "Onboard X", "description": "Run script"})
        self.assertEqual(text, "Onboard X\n\nRun script")

    def test_falls_back_to_payload(self):
        text = goal_cache.format_goal({"payload": {"title": "P", "description": "Q"}})
        self.assertEqual(text, "P\n\nQ")

    def test_caps_at_500_chars(self):
        text = goal_cache.format_goal({"title": "T", "description": "x" * 1000})
        self.assertLessEqual(len(text), goal_cache.GOAL_TEXT_MAX_CHARS)
        self.assertTrue(text.endswith("…"))

    def test_strips_control_chars_and_caps_lines(self):
        desc = "\n".join(f"line{i}\x07" for i in range(40))
        text = goal_cache.format_goal({"title": "T", "description": desc})
        self.assertNotIn("\x07", text)
        self.assertLessEqual(len(text.splitlines()), goal_cache.GOAL_TEXT_MAX_LINES)

    def test_empty_meta(self):
        self.assertEqual(goal_cache.format_goal({}), "")
        self.assertEqual(goal_cache.format_goal(None), "")


if __name__ == "__main__":
    unittest.main()
