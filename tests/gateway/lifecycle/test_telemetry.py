"""§8 context telemetry — ContextUsage normalization + companion store."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.lifecycle import telemetry  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    telemetry.init_db(conn)
    return conn


class ContextUsageTest(unittest.TestCase):
    def test_effective_is_input_plus_cache(self) -> None:
        usage = telemetry.ContextUsage.from_anthropic_usage(
            {
                "input_tokens": 1000,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 50_000,
                "output_tokens": 800,
            }
        )
        self.assertEqual(usage.effective_input_tokens, 51_200)
        self.assertFalse(usage.is_zero)

    def test_missing_usage_is_zero(self) -> None:
        usage = telemetry.ContextUsage.from_anthropic_usage({})
        self.assertIsNone(usage.effective_input_tokens)
        self.assertTrue(usage.is_zero)


class StoreTest(unittest.TestCase):
    def test_record_usage_then_read(self) -> None:
        conn = _conn()
        usage = telemetry.ContextUsage.from_anthropic_usage(
            {"input_tokens": 10_000, "output_tokens": 500}, source="api"
        )
        tel = telemetry.record_usage(
            conn, owner_key="k", brain="claude", usage=usage, model="claude-opus-4-8"
        )
        self.assertEqual(tel.effective_input_tokens, 10_000)
        self.assertEqual(tel.turn_count, 1)
        self.assertEqual(tel.usage_source, "api")
        self.assertEqual(tel.last_model, "claude-opus-4-8")

    def test_zero_usage_keeps_prior_tokens_but_bumps_turn(self) -> None:
        conn = _conn()
        good = telemetry.ContextUsage.from_anthropic_usage(
            {"input_tokens": 42_000}, source="api"
        )
        telemetry.record_usage(conn, owner_key="k", brain="claude", usage=good)
        zero = telemetry.ContextUsage(None, None, None, None, None, "estimate", telemetry.now_iso())
        tel = telemetry.record_usage(conn, owner_key="k", brain="claude", usage=zero)
        self.assertEqual(tel.effective_input_tokens, 42_000)  # preserved
        self.assertEqual(tel.usage_source, "api")  # preserved
        self.assertEqual(tel.turn_count, 2)  # advanced

    def test_record_rotation_resets_tokens_and_counts(self) -> None:
        conn = _conn()
        usage = telemetry.ContextUsage.from_anthropic_usage({"input_tokens": 9_000})
        telemetry.record_usage(conn, owner_key="k", brain="claude", usage=usage)
        telemetry.record_rotation(conn, owner_key="k")
        tel = telemetry.get_telemetry(conn, owner_key="k")
        self.assertIsNotNone(tel)
        assert tel is not None
        self.assertIsNone(tel.effective_input_tokens)
        self.assertEqual(tel.rotation_count, 1)
        self.assertEqual(tel.maintenance_state, "rotated")

    def test_list_orders_by_effective_tokens_desc(self) -> None:
        conn = _conn()
        for key, toks in (("a", 5_000), ("b", 90_000), ("c", 40_000)):
            usage = telemetry.ContextUsage.from_anthropic_usage({"input_tokens": toks})
            telemetry.record_usage(conn, owner_key=key, brain="claude", usage=usage)
        rows = telemetry.list_telemetry(conn)
        self.assertEqual([r.owner_key for r in rows], ["b", "c", "a"])

    def test_record_usage_concurrent_turn_count_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "queue.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            telemetry.init_db(conn)
            conn.close()

            barrier = threading.Barrier(2)
            errors: list[BaseException] = []

            def worker() -> None:
                worker_conn = sqlite3.connect(db, timeout=5.0)
                worker_conn.row_factory = sqlite3.Row
                try:
                    usage = telemetry.ContextUsage.from_anthropic_usage(
                        {"input_tokens": 100}, source="api"
                    )
                    barrier.wait(timeout=5.0)
                    telemetry.record_usage(
                        worker_conn, owner_key="k", brain="claude", usage=usage
                    )
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                finally:
                    worker_conn.close()

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10.0)

            self.assertFalse(errors)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                tel = telemetry.get_telemetry(conn, owner_key="k")
            finally:
                conn.close()
            self.assertIsNotNone(tel)
            assert tel is not None
            self.assertEqual(tel.turn_count, 2)


if __name__ == "__main__":
    unittest.main()
