"""Tests for backpressure and log rotation behavior."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue  # noqa: E402
from gateway.config import render_default_config  # noqa: E402
from gateway.runtime import GatewayRuntime  # noqa: E402


def _make_instance(tmp: str, *, max_depth: int = 2) -> Path:
    instance = Path(tmp)
    (instance / ".jc").write_text("", encoding="utf-8")
    (instance / "ops").mkdir()
    (instance / "memory" / "L1").mkdir(parents=True)
    base = render_default_config(default_brain="claude")
    yaml = base + f"reliability:\n  max_queue_depth: {max_depth}\n"
    (instance / "ops" / "gateway.yaml").write_text(yaml, encoding="utf-8")
    return instance


class BackpressureTests(unittest.TestCase):
    def test_drops_when_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, max_depth=2)
            runtime = GatewayRuntime(
                instance,
                log_path=queue.queue_dir(instance) / "test.log",
                stop_requested=lambda: True,
            )
            runtime.enqueue(source="manual", content="a")
            runtime.enqueue(source="manual", content="b")
            # Third should be dropped silently.
            runtime.enqueue(source="manual", content="c")

            conn = queue.connect(instance)
            try:
                rows = conn.execute("SELECT content FROM events ORDER BY id").fetchall()
            finally:
                conn.close()
            contents = [row["content"] for row in rows]
            self.assertEqual(contents, ["a", "b"])


class JsonLogTests(unittest.TestCase):
    def test_log_lines_are_json_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, max_depth=10)
            log_path = queue.queue_dir(instance) / "test.log"
            runtime = GatewayRuntime(
                instance,
                log_path=log_path,
                stop_requested=lambda: True,
            )
            runtime.enqueue(source="manual", content="hi")
            runtime.log("hello world", event_id=42, brain="claude:opus-4-7-1m")
            for handler in list(runtime._json_logger.handlers):
                handler.flush()

            lines = [
                line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()
            ]
            self.assertTrue(lines, "log file empty")
            for line in lines:
                payload = json.loads(line)
                self.assertIn("ts", payload)
                self.assertIn("level", payload)
                self.assertIn("msg", payload)


if __name__ == "__main__":
    unittest.main()
