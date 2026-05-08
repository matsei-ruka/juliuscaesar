"""Tests for `gateway.context.render_clock` / `render_clock_inline`.

Covers docs/specs/timezone-config.md §Runtime injection:

- The clock block carries the IANA zone name, an offset like UTC+04:00,
  and an ISO 8601 timestamp.
- The function MUST evaluate `datetime.now(...)` on each call (no caching).
- An unknown zone bubbles an exception — the validator catches this at
  config load time, so render-time failure is acceptable.
- The inline form is single-line and starts with `[Current time:`.
"""

from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import context  # noqa: E402


class RenderClockTests(unittest.TestCase):
    def test_utc_block_shape(self):
        text = context.render_clock("UTC")
        self.assertTrue(text.startswith("# Current time\n"))
        self.assertIn("UTC", text)
        self.assertIn("UTC+00:00", text)
        # Has YYYY-MM-DD HH:MM token.
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
        self.assertIn("ISO 8601:", text)

    def test_dubai_block_offset(self):
        text = context.render_clock("Asia/Dubai")
        self.assertIn("Asia/Dubai", text)
        self.assertIn("UTC+04:00", text)
        # ISO 8601 component must include the +04:00 offset.
        self.assertRegex(text, r"\+04:00")

    def test_negative_offset_zone(self):
        text = context.render_clock("America/New_York")
        self.assertIn("America/New_York", text)
        # Either UTC-04:00 (DST) or UTC-05:00 — accept both.
        self.assertRegex(text, r"UTC-0[45]:00")

    def test_empty_falls_back_to_utc(self):
        text = context.render_clock("")
        self.assertIn("UTC", text)

    def test_not_cached(self):
        # Patch datetime.now used inside context module to return two
        # distinct fixed instants; if the helper cached the result, the
        # second call would echo the first.
        first = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        second = first + timedelta(hours=1)
        captured = []

        class _FakeDatetime:
            @classmethod
            def now(cls, tz=None):
                ts = captured.pop(0)
                return ts.astimezone(tz) if tz is not None else ts

        captured.extend([first, second])
        with mock.patch.object(context, "datetime", _FakeDatetime):
            a = context.render_clock("UTC")
            b = context.render_clock("UTC")
        self.assertNotEqual(a, b)
        self.assertIn("12:00", a)
        self.assertIn("13:00", b)

    def test_unknown_zone_raises(self):
        with self.assertRaises(Exception):
            context.render_clock("Foo/Bar")


class RenderClockInlineTests(unittest.TestCase):
    def test_inline_shape(self):
        text = context.render_clock_inline("Asia/Dubai")
        self.assertTrue(text.startswith("[Current time:"))
        self.assertTrue(text.endswith(")]"))
        self.assertIn("Asia/Dubai", text)
        self.assertIn("UTC+04:00", text)
        self.assertNotIn("\n", text)

    def test_inline_utc_offset(self):
        text = context.render_clock_inline("UTC")
        self.assertIn("UTC+00:00", text)


if __name__ == "__main__":
    unittest.main()
