"""Tests for `memory.accountabilities_audit.append_audit_entry`.

Covers docs/specs/accountabilities.md §Phase 4:
- First append creates the file with frontmatter + table header + 1 row.
- Subsequent appends preserve existing rows and order.
- Frontmatter stays intact and at the top across many appends.
- Each row contains all 4 fields (timestamp, change, source, token).
- The L2 accountabilities directory is created when absent.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from memory.accountabilities_audit import (  # noqa: E402
    AuditEntry,
    append_audit_entry,
    audit_path,
)


def _make_entry(
    *,
    timestamp: str = "2026-05-15T10:00:00Z",
    change: str = 'Added accountability "Vendor escalation framing"',
    source_chat_id: str = "28547271",
    source_message_id: str = "7032",
    token_observed: str = "OK enact",
) -> AuditEntry:
    return AuditEntry(
        timestamp=timestamp,
        change=change,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        token_observed=token_observed,
    )


class AppendAuditEntryTests(unittest.TestCase):
    def test_creates_file_on_first_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            target = audit_path(instance)
            self.assertFalse(target.exists())

            append_audit_entry(instance, _make_entry())

            self.assertTrue(target.exists())
            body = target.read_text(encoding="utf-8")
            self.assertIn("slug: accountabilities-audit", body)
            self.assertIn("# Accountabilities audit log", body)
            self.assertIn("| Timestamp | Change |", body)
            # Header row, separator row, and 1 data row each start with `|`.
            self.assertEqual(body.count("\n|"), 3)

    def test_appends_preserves_existing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            first = _make_entry(change="First change", timestamp="2026-05-15T10:00:00Z")
            second = _make_entry(change="Second change", timestamp="2026-05-15T11:00:00Z")

            append_audit_entry(instance, first)
            append_audit_entry(instance, second)

            body = audit_path(instance).read_text(encoding="utf-8")
            idx_first = body.find("First change")
            idx_second = body.find("Second change")
            self.assertGreater(idx_first, 0)
            self.assertGreater(idx_second, idx_first)

    def test_frontmatter_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            for i in range(3):
                append_audit_entry(
                    instance,
                    _make_entry(change=f"change-{i}", timestamp=f"2026-05-15T10:0{i}:00Z"),
                )
            body = audit_path(instance).read_text(encoding="utf-8")
            lines = body.splitlines()
            self.assertEqual(lines[0], "---")
            self.assertEqual(lines[1], "slug: accountabilities-audit")
            self.assertEqual(lines[2], "type: audit-log")
            self.assertEqual(lines[3], "state: active")
            self.assertEqual(lines[4], "---")
            # Frontmatter exactly once.
            self.assertEqual(body.count("\n---\n"), 1)

    def test_row_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            entry = _make_entry(
                timestamp="2026-05-15T10:00:00Z",
                change='Added "Vendor escalation"',
                source_chat_id="28547271",
                source_message_id="7032",
                token_observed="OK enact",
            )
            append_audit_entry(instance, entry)
            body = audit_path(instance).read_text(encoding="utf-8")
            last_row = body.strip().splitlines()[-1]
            self.assertIn("2026-05-15T10:00:00Z", last_row)
            self.assertIn('Added "Vendor escalation"', last_row)
            self.assertIn("28547271", last_row)
            self.assertIn("7032", last_row)
            self.assertIn("OK enact", last_row)
            self.assertTrue(last_row.startswith("|") and last_row.endswith("|"))

    def test_creates_l2_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            l2_dir = instance / "memory" / "L2" / "accountabilities"
            self.assertFalse(l2_dir.exists())

            append_audit_entry(instance, _make_entry())

            self.assertTrue(l2_dir.is_dir())


if __name__ == "__main__":
    unittest.main()
