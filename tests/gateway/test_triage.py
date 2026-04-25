"""Tests for the triage layer."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.config import TriageConfig  # noqa: E402
from gateway.triage import MetricsRecorder, TriageCache  # noqa: E402
from gateway.triage.base import (  # noqa: E402
    TriageResult,
    parse_triage_json,
    render_prompt,
)
from gateway.triage.factory import build_backend  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_parse_clean_json(self):
        result = parse_triage_json('{"class":"analysis","brain":"claude:opus-4-7-1m","confidence":0.91}')
        self.assertEqual(result.class_, "analysis")
        self.assertEqual(result.brain, "claude:opus-4-7-1m")
        self.assertAlmostEqual(result.confidence, 0.91)

    def test_parse_with_chatter(self):
        text = "Sure thing!\n  {\"class\":\"smalltalk\",\"brain\":\"claude:haiku-4-5\",\"confidence\":0.95}\n"
        result = parse_triage_json(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.class_, "smalltalk")

    def test_parse_invalid(self):
        self.assertIsNone(parse_triage_json("not json"))
        self.assertIsNone(parse_triage_json('{"class":"smalltalk"}'))

    def test_unknown_class_falls_back_to_quick(self):
        result = parse_triage_json('{"class":"weird","brain":"claude:sonnet-4-6","confidence":0.8}')
        self.assertEqual(result.class_, "quick")

    def test_clamps_confidence(self):
        result = parse_triage_json('{"class":"code","brain":"claude:sonnet-4-6","confidence":2.0}')
        self.assertEqual(result.confidence, 1.0)


class PromptTests(unittest.TestCase):
    def test_message_inserted(self):
        text = render_prompt("hello world")
        self.assertIn("hello world", text)
        self.assertIn("Schema:", text)


class CacheTests(unittest.TestCase):
    def test_hit_within_ttl(self):
        cache = TriageCache(ttl_seconds=5)
        result = TriageResult(class_="quick", brain="claude:sonnet-4-6", confidence=0.9)
        cache.put("hi", result)
        self.assertEqual(cache.get("hi"), result)

    def test_miss_after_ttl(self):
        cache = TriageCache(ttl_seconds=0)
        cache.put("hi", TriageResult("quick", "claude", 0.9))
        self.assertIsNone(cache.get("hi"))

    def test_eviction_on_overflow(self):
        cache = TriageCache(ttl_seconds=60, max_entries=2)
        cache.put("a", TriageResult("quick", "claude", 0.9))
        cache.put("b", TriageResult("quick", "claude", 0.9))
        cache.put("c", TriageResult("quick", "claude", 0.9))
        self.assertIsNone(cache.get("a"))
        self.assertIsNotNone(cache.get("b"))
        self.assertIsNotNone(cache.get("c"))


class FactoryTests(unittest.TestCase):
    def test_none_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = build_backend(TriageConfig(backend="none"), Path(tmp))
            self.assertEqual(backend.name, "none")

    def test_unknown_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                build_backend(TriageConfig(backend="bogus"), Path(tmp))


class MetricsTests(unittest.TestCase):
    def test_record_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = MetricsRecorder(Path(tmp))
            recorder.record(TriageResult("smalltalk", "claude:haiku-4-5", 0.95))
            recorder.record(TriageResult("smalltalk", "claude:haiku-4-5", 0.85))
            recorder.record(TriageResult("analysis", "claude:opus-4-7-1m", 0.4), fallback=True)
            summary = recorder.summary(hours=1)
            classes = {row["class"]: row for row in summary["by_class"]}
            self.assertEqual(classes["smalltalk"]["count"], 2)
            self.assertEqual(classes["analysis"]["fallbacks"], 1)


if __name__ == "__main__":
    unittest.main()
