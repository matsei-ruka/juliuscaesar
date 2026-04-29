"""Tests for the hot_tidy heartbeat builtin."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from heartbeat.builtins import hot_tidy  # noqa: E402


HOT_WITH_OVERFLOW = """---
slug: HOT
title: Hot Cache (last 7 days)
layer: L1
type: hot
state: draft
created: 2026-04-01
updated: 2026-04-29
last_verified: ""
tags: [hot]
links: []
---

# Hot cache — rolling 7-day context (today 2026-04-01)

What's alive right now.

## What shipped today (2026-04-29)

**PR #30 — A.** Detail one. Impact alpha.

**PR #29 — B.** Detail two. Impact beta.

**PR #28 — C.** Detail three. Impact gamma.

**PR #27 — D.** Detail four. Impact delta.

**PR #26 — E.** Detail five. Impact epsilon.

**PR #25 — F.** Old item, should be archived.

**PR #24 — G.** Even older, should be archived.

## Immediate open threads

- Thread 1 with status.
- Thread 2 with status.
- Thread 3 with status.

## Known nuisances

- Nuisance one.
- Nuisance two.
"""


def _make_instance(hot_text: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix="hot-tidy-test-"))
    (root / "memory" / "L1").mkdir(parents=True)
    (root / "memory" / "L2").mkdir(parents=True)
    (root / "memory" / "L1" / "HOT.md").write_text(hot_text, encoding="utf-8")
    return root


class HotTidyParseTest(unittest.TestCase):
    def test_parse_recognizes_sections(self) -> None:
        parsed = hot_tidy.parse(HOT_WITH_OVERFLOW)
        self.assertEqual(len(parsed.sections), 3)
        canonical = [s.canonical for s in parsed.sections]
        self.assertEqual(canonical, ["what_shipped", "open_threads", "known_nuisances"])

    def test_parse_paragraph_items(self) -> None:
        parsed = hot_tidy.parse(HOT_WITH_OVERFLOW)
        shipped = parsed.sections[0]
        self.assertEqual(shipped.shape, "paragraph")
        self.assertEqual(len(shipped.items), 7)
        self.assertIn("PR #30", shipped.items[0])
        self.assertIn("PR #24", shipped.items[6])

    def test_parse_bullet_items(self) -> None:
        parsed = hot_tidy.parse(HOT_WITH_OVERFLOW)
        threads = parsed.sections[1]
        self.assertEqual(threads.shape, "bullet")
        self.assertEqual(len(threads.items), 3)
        self.assertTrue(threads.items[0].lstrip().startswith("-"))


class HotTidyRunTest(unittest.TestCase):
    def test_dry_run_does_not_write(self) -> None:
        instance = _make_instance(HOT_WITH_OVERFLOW)
        original = (instance / hot_tidy.HOT_MD_PATH).read_text(encoding="utf-8")
        summary = hot_tidy.run(instance, dry_run=True)
        self.assertTrue(summary["ok"])
        self.assertFalse(summary["written"])
        self.assertEqual(len(summary["archived"]), 2)
        # File untouched.
        self.assertEqual(
            (instance / hot_tidy.HOT_MD_PATH).read_text(encoding="utf-8"),
            original,
        )
        # No L2 files written either.
        self.assertEqual(list((instance / "memory" / "L2").rglob("*.md")), [])

    def test_run_archives_overflow_and_rewrites(self) -> None:
        instance = _make_instance(HOT_WITH_OVERFLOW)
        summary = hot_tidy.run(instance, dry_run=False)
        self.assertTrue(summary["ok"])
        self.assertTrue(summary["written"])
        self.assertEqual(len(summary["archived"]), 2)

        # HOT.md trimmed to 5 items in shipped section.
        new_text = (instance / hot_tidy.HOT_MD_PATH).read_text(encoding="utf-8")
        parsed = hot_tidy.parse(new_text)
        self.assertEqual(len(parsed.sections[0].items), 5)
        self.assertNotIn("PR #25", new_text)
        self.assertNotIn("PR #24", new_text)
        # Newest 5 still present.
        self.assertIn("PR #30", new_text)
        self.assertIn("PR #26", new_text)

        # Archive entries exist with correct frontmatter.
        archived_dir = instance / "memory" / "L2" / "completed"
        archived_files = list(archived_dir.glob("*.md"))
        self.assertEqual(len(archived_files), 2)
        body = archived_files[0].read_text(encoding="utf-8")
        self.assertIn("layer: L2", body)
        self.assertIn("type: completed", body)
        self.assertIn("archived_from: HOT.md/what_shipped", body)
        self.assertIn("state: archived", body)

    def test_today_stamp_updated(self) -> None:
        instance = _make_instance(HOT_WITH_OVERFLOW)
        hot_tidy.run(instance, dry_run=False)
        new_text = (instance / hot_tidy.HOT_MD_PATH).read_text(encoding="utf-8")
        # Header (today YYYY-MM-DD) is updated to today's date.
        import datetime as dt

        self.assertIn(f"(today {dt.date.today().isoformat()})", new_text)

    def test_no_overflow_is_no_op(self) -> None:
        text = """---
slug: HOT
title: Hot
layer: L1
---

# Hot cache

## What shipped

- A
- B

## Open threads

- T1

## Known nuisances

- N1
"""
        instance = _make_instance(text)
        summary = hot_tidy.run(instance, dry_run=False)
        self.assertEqual(summary["archived"], [])

    def test_missing_hot_md_returns_not_ok(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="hot-tidy-test-"))
        summary = hot_tidy.run(root, dry_run=False)
        self.assertFalse(summary["ok"])
        self.assertIn("HOT.md not found", summary["reason"])

    def test_slug_collision_increments_counter(self) -> None:
        # Force two overflow items with identical first lines.
        text = """# Hot cache

## What shipped

**PR #1.** Body A.

**PR #1.** Body B.

**PR #1.** Body C.

**PR #1.** Body D.

**PR #1.** Body E.

**PR #1.** Body F overflow.

**PR #1.** Body G overflow.
"""
        instance = _make_instance(text)
        summary = hot_tidy.run(instance, dry_run=False)
        archived_dir = instance / "memory" / "L2" / "completed"
        files = sorted(p.name for p in archived_dir.glob("*.md"))
        self.assertEqual(len(files), 2)
        # Filenames must differ even though titles match.
        self.assertNotEqual(files[0], files[1])
        self.assertEqual(len(summary["archived"]), 2)


class HotTidyRunnerIntegrationTest(unittest.TestCase):
    """Confirm the runner dispatches builtin tasks correctly."""

    def test_runner_invokes_builtin_when_disabled_uses_dry_run(self) -> None:
        from heartbeat.runner import run_task

        instance = _make_instance(HOT_WITH_OVERFLOW)
        (instance / "heartbeat").mkdir()
        (instance / "heartbeat" / "tasks.yaml").write_text(
            """defaults: {}
tasks:
  hot_tidy:
    builtin: hot_tidy
    enabled: false
""",
            encoding="utf-8",
        )
        rc = run_task(instance, "hot_tidy", dry_run=False)
        self.assertEqual(rc, 0)
        # Disabled → ran as dry-run, HOT.md untouched.
        new_text = (instance / hot_tidy.HOT_MD_PATH).read_text(encoding="utf-8")
        self.assertIn("PR #25", new_text)
        self.assertIn("PR #24", new_text)
        # Output JSON written.
        outputs = list((instance / "heartbeat" / "state" / "outputs").glob("hot_tidy-*.json"))
        self.assertEqual(len(outputs), 1)

    def test_runner_invokes_builtin_when_enabled(self) -> None:
        from heartbeat.runner import run_task

        instance = _make_instance(HOT_WITH_OVERFLOW)
        (instance / "heartbeat").mkdir()
        (instance / "heartbeat" / "tasks.yaml").write_text(
            """defaults: {}
tasks:
  hot_tidy:
    builtin: hot_tidy
    enabled: true
""",
            encoding="utf-8",
        )
        rc = run_task(instance, "hot_tidy", dry_run=False)
        self.assertEqual(rc, 0)
        new_text = (instance / hot_tidy.HOT_MD_PATH).read_text(encoding="utf-8")
        self.assertNotIn("PR #25", new_text)


if __name__ == "__main__":
    unittest.main()
