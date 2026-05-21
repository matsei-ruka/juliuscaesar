"""Pre-filter tests for the smart slot classifier.

See `docs/specs/parallel-slots-smart-classifier.md`. The pre-filter is the
fast deterministic gate that catches obvious topic shifts before the LLM
classifier runs. These tests exercise both the module-level helpers
(`_tokenize`, `_jaccard`, `_extract_entities`) and the runtime method
(`_prefilter_slot_affinity`).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue  # noqa: E402
from gateway.config import render_default_config  # noqa: E402
from gateway.runtime import (  # noqa: E402
    GatewayRuntime,
    _extract_entities,
    _jaccard,
    _tokenize,
)


def _config(max_concurrent: int) -> str:
    text = render_default_config(default_brain="claude:sonnet-4-6")
    return text.replace(
        "parallel:\n  max_concurrent: 1",
        f"parallel:\n  max_concurrent: {max_concurrent}",
    )


def _instance(max_concurrent: int = 2) -> Path:
    root = Path(tempfile.mkdtemp(prefix="jc-prefilter-"))
    (root / ".jc").write_text("", encoding="utf-8")
    (root / "ops").mkdir()
    (root / "ops" / "gateway.yaml").write_text(_config(max_concurrent), encoding="utf-8")
    return root


def _build_runtime(instance: Path) -> GatewayRuntime:
    return GatewayRuntime(
        instance,
        log_path=queue.queue_dir(instance) / "test.log",
        stop_requested=lambda: True,
    )


class TokenizeTests(unittest.TestCase):
    def test_tokenize_removes_stopwords(self) -> None:
        result = _tokenize("what is the status of X")
        # "what","is","the","of" are stopwords. "status" + "x" remain.
        self.assertIn("status", result)
        self.assertIn("x", result)
        self.assertNotIn("what", result)
        self.assertNotIn("the", result)
        self.assertNotIn("of", result)

    def test_tokenize_empty_returns_empty(self) -> None:
        self.assertEqual(_tokenize(""), frozenset())

    def test_tokenize_strips_punctuation(self) -> None:
        result = _tokenize("hello, world! foo.bar")
        self.assertIn("hello", result)
        self.assertIn("world", result)


class JaccardTests(unittest.TestCase):
    def test_jaccard_basic(self) -> None:
        a = frozenset({"a", "b", "c"})
        b = frozenset({"b", "c", "d"})
        result = _jaccard(a, b)
        # intersection={"b","c"}, union={"a","b","c","d"} → 2/4 = 0.5
        self.assertIsInstance(result, float)
        self.assertAlmostEqual(result, 0.5)

    def test_jaccard_empty_returns_zero(self) -> None:
        self.assertEqual(_jaccard(frozenset(), frozenset({"a"})), 0.0)
        self.assertEqual(_jaccard(frozenset({"a"}), frozenset()), 0.0)
        self.assertEqual(_jaccard(frozenset(), frozenset()), 0.0)

    def test_jaccard_identical_is_one(self) -> None:
        a = frozenset({"a", "b"})
        self.assertEqual(_jaccard(a, a), 1.0)

    def test_jaccard_disjoint_is_zero(self) -> None:
        a = frozenset({"a", "b"})
        b = frozenset({"c", "d"})
        self.assertEqual(_jaccard(a, b), 0.0)


class ExtractEntitiesTests(unittest.TestCase):
    def test_extract_entities_title_case(self) -> None:
        result = _extract_entities("Check Florian and Sophie")
        # "Check" is a common verb and must be filtered out.
        self.assertEqual(result, frozenset({"Florian", "Sophie"}))

    def test_extract_entities_allcaps(self) -> None:
        result = _extract_entities("BNESIM status")
        self.assertIn("BNESIM", result)

    def test_extract_entities_vm_ip(self) -> None:
        result = _extract_entities("restart VM103 at 192.168.14.103")
        self.assertIn("VM103", result)
        self.assertIn("192.168.14.103", result)

    def test_extract_entities_empty_text(self) -> None:
        self.assertEqual(_extract_entities(""), frozenset())


class PrefilterSlotAffinityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.instance = _instance(max_concurrent=2)
        self.runtime = _build_runtime(self.instance)

    def tearDown(self) -> None:
        self.runtime.close()

    def test_prefilter_obvious_unrelated(self) -> None:
        # "meteo dubai" vs slot 0 about Florian gateway restart
        verdict, slot = self.runtime._prefilter_slot_affinity(
            "meteo dubai?",
            {0: "Florian gateway restart PID watchdog"},
        )
        self.assertEqual(verdict, "unrelated")
        self.assertIsNone(slot)

    def test_prefilter_obvious_unrelated_bnesim(self) -> None:
        # "what about bnesim" — single content token, no overlap with slot
        verdict, slot = self.runtime._prefilter_slot_affinity(
            "what about bnesim",
            {0: "gateway restart watchdog"},
        )
        self.assertEqual(verdict, "unrelated")
        self.assertIsNone(slot)

    def test_prefilter_same_topic_ambiguous(self) -> None:
        # "restart sophie again" vs slot about Sophie restart → high Jaccard
        # → LLM gets to verify.
        verdict, _slot = self.runtime._prefilter_slot_affinity(
            "restart sophie again",
            {0: "sophie reinhardt VM restart"},
        )
        self.assertEqual(verdict, "ambiguous")

    def test_prefilter_new_entity_unrelated(self) -> None:
        # "restart florian" while slot is about Sophie → Jaccard 0 → unrelated
        verdict, slot = self.runtime._prefilter_slot_affinity(
            "restart florian",
            {0: "Sophie reinhardt instance"},
        )
        self.assertEqual(verdict, "unrelated")
        self.assertIsNone(slot)

    def test_prefilter_empty_summaries_ambiguous(self) -> None:
        # No history yet — pre-filter shouldn't claim "unrelated".
        verdict, _slot = self.runtime._prefilter_slot_affinity("anything", {})
        self.assertEqual(verdict, "ambiguous")


if __name__ == "__main__":
    unittest.main()
