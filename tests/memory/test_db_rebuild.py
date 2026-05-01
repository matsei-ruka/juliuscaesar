"""Tests for `memory.db.rebuild` invalid-state handling."""

from __future__ import annotations

import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from memory import db as memory_db  # noqa: E402


def _write_entry(
    instance: Path,
    *,
    layer: str,
    slug: str,
    state: str = "draft",
    title: str = "Test",
) -> Path:
    """Create memory/<layer>/<slug>.md with the given frontmatter state."""
    target = instance / "memory" / layer / f"{slug}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\n"
        f"slug: {slug}\n"
        f"title: {title}\n"
        f"layer: {layer}\n"
        f"state: {state}\n"
        "---\n"
        "Body.\n"
    )
    return target


class RebuildInvalidStateTests(unittest.TestCase):
    def test_invalid_state_raises_in_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            bad = _write_entry(inst, layer="L2", slug="bad", state="published")
            with self.assertRaises(ValueError) as cm:
                memory_db.parse_markdown(bad, inst)
            self.assertIn("invalid state", str(cm.exception))
            self.assertIn("published", str(cm.exception))

    def test_rebuild_skips_invalid_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            _write_entry(inst, layer="L2", slug="good-1", state="verified")
            _write_entry(inst, layer="L2", slug="bad", state="published")
            _write_entry(inst, layer="L2", slug="good-2", state="draft")
            conn = memory_db.connect(inst)
            try:
                buf = StringIO()
                old = sys.stderr
                sys.stderr = buf
                try:
                    upserted, removed, skipped = memory_db.rebuild(conn, inst)
                finally:
                    sys.stderr = old
            finally:
                conn.close()
            self.assertEqual(upserted, 2)
            self.assertEqual(skipped, 1)
            self.assertEqual(removed, 0)
            self.assertIn("[skip]", buf.getvalue())
            self.assertIn("published", buf.getvalue())

    def test_rebuild_all_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            for s, st in [
                ("a", "draft"),
                ("b", "reviewed"),
                ("c", "verified"),
                ("d", "active"),
                ("e", "stale"),
                ("f", "archived"),
            ]:
                _write_entry(inst, layer="L2", slug=s, state=st)
            conn = memory_db.connect(inst)
            try:
                upserted, removed, skipped = memory_db.rebuild(conn, inst)
            finally:
                conn.close()
            self.assertEqual(upserted, 6)
            self.assertEqual(skipped, 0)

    def test_active_state_is_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            target = _write_entry(inst, layer="L2", slug="active-note", state="active")
            entry = memory_db.parse_markdown(target, inst)
            self.assertEqual(entry.state, "active")

    def test_connect_migrates_old_schema_without_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            db = inst / "memory" / "index.sqlite"
            db.parent.mkdir(parents=True, exist_ok=True)
            import sqlite3

            conn = sqlite3.connect(db)
            try:
                conn.executescript(
                    """
                    CREATE TABLE entries (
                        slug TEXT PRIMARY KEY,
                        state TEXT DEFAULT 'draft' CHECK (state IN ('draft','reviewed','verified','stale','archived'))
                    );
                    """
                )
            finally:
                conn.close()

            conn = memory_db.connect(inst)
            try:
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='entries'"
                ).fetchone()
                self.assertIn("'active'", row["sql"])
            finally:
                conn.close()

    def test_default_state_is_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            target = inst / "memory" / "L2" / "no-state.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "---\nslug: no-state\ntitle: T\nlayer: L2\n---\nBody.\n"
            )
            entry = memory_db.parse_markdown(target, inst)
            self.assertEqual(entry.state, "draft")

    def test_valid_states_constant_matches_schema(self):
        """Drift guard: VALID_STATES must match the SQL CHECK constraint."""
        # Extract the IN-list from the schema string. Brittle by design — if
        # someone edits the CHECK clause, this test is the loudest place to
        # remember to update VALID_STATES too.
        import re
        m = re.search(
            r"state\s+TEXT[^,]*CHECK\s*\(\s*state\s+IN\s*\(([^)]+)\)\s*\)",
            memory_db.SCHEMA,
        )
        self.assertIsNotNone(m, "schema CHECK clause not found")
        sql_states = {
            v.strip().strip("'").strip('"')
            for v in m.group(1).split(",")
        }
        self.assertEqual(sql_states, set(memory_db.VALID_STATES))

    def test_noindex_flag_skips_parse(self):
        """noindex: true in frontmatter returns None (not indexed)."""
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            target = inst / "memory" / "L2" / "operational.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "---\nslug: operational\ntitle: T\nlayer: L2\nnoindex: true\n---\nBody.\n"
            )
            entry = memory_db.parse_markdown(target, inst)
            self.assertIsNone(entry)

    def test_rebuild_skips_noindex_silently(self):
        """rebuild() skips noindex files without counting them."""
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            _write_entry(inst, layer="L2", slug="indexed", state="draft")
            # Operational file with noindex: true
            noindex_file = inst / "memory" / "L2" / "operational.md"
            noindex_file.parent.mkdir(parents=True, exist_ok=True)
            noindex_file.write_text(
                "---\nslug: operational\ntitle: Op\nlayer: L2\nnoindex: true\n---\nState.\n"
            )
            conn = memory_db.connect(inst)
            try:
                upserted, removed, skipped = memory_db.rebuild(conn, inst)
            finally:
                conn.close()
            # Only the indexed entry is upserted; noindex file is silent
            self.assertEqual(upserted, 1)
            self.assertEqual(skipped, 0)
            # Verify operational file is NOT in DB
            conn = memory_db.connect(inst)
            try:
                row = memory_db.get(conn, "operational")
                self.assertIsNone(row)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
