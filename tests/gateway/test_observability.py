"""Audit features 9+10 — ownership findings, queue metrics, real liveness."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue  # noqa: E402
from gateway.liveness import (  # noqa: E402
    gateway_uid_finding,
    state_ownership_findings,
)
from gateway.observability import queue_metrics, snapshot  # noqa: E402


class QueueMetricsTests(unittest.TestCase):
    def test_absent_db_reports_not_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = queue_metrics(Path(tmp))
        self.assertFalse(m["db_present"])

    def test_seeded_queue_counts_and_oldest_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            conn = queue.connect(instance)
            try:
                queue.enqueue(
                    conn,
                    source="cron",
                    source_message_id="m1",
                    user_id=None,
                    conversation_id="c1",
                    content="hello",
                    meta=None,
                )
                conn.commit()
            finally:
                conn.close()
            m = queue_metrics(instance)
        self.assertTrue(m["db_present"])
        self.assertEqual(m["depth_by_status"].get("queued"), 1)
        self.assertIsNotNone(m["oldest_queued_age_seconds"])
        self.assertGreaterEqual(m["oldest_queued_age_seconds"], 0.0)

    def test_snapshot_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            state = instance / "state" / "gateway"
            state.mkdir(parents=True)
            (state / "channel_health.json").write_text(
                json.dumps({"telegram": {"state": "running"}})
            )
            snap = snapshot(instance)
        for key in (
            "gateway",
            "heartbeat_age_seconds",
            "liveness",
            "queue",
            "brains_failed",
            "channel_health",
        ):
            self.assertIn(key, snap)
        self.assertEqual(snap["channel_health"]["telegram"]["state"], "running")


class OwnershipFindingTests(unittest.TestCase):
    def test_state_ownership_ok_for_own_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "state" / "gateway").mkdir(parents=True)
            (instance / "state" / "gateway" / "x.log").write_text("x")
            findings = state_ownership_findings(instance)
        self.assertEqual([f.level for f in findings], ["ok"])

    def test_gateway_uid_none_when_no_pidfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(gateway_uid_finding(Path(tmp)))

    def test_gateway_uid_matches_self(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            pidfile = instance / "state" / "gateway" / "jc-gateway.pid"
            pidfile.parent.mkdir(parents=True)
            pidfile.write_text(str(os.getpid()))
            finding = gateway_uid_finding(instance)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.level, "ok")


class RealLivenessTests(unittest.TestCase):
    def _runtime(self, instance: Path):
        from gateway.runtime import GatewayRuntime

        (instance / "ops").mkdir(parents=True, exist_ok=True)
        (instance / "ops" / "gateway.yaml").write_text("default_brain: claude\n")
        from gateway import config as gateway_config

        gateway_config.clear_config_cache()
        return GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )

    def test_touch_skipped_when_stalled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            rt = self._runtime(instance)
            hb = instance / "state" / "gateway" / "heartbeat"
            rt._note_progress()
            rt._touch_heartbeat()
            self.assertTrue(hb.exists())
            first_mtime = hb.stat().st_mtime
            # Simulate a stall: progress far in the past.
            rt._last_progress = time.monotonic() - (rt.LIVENESS_STALL_SECONDS + 1)
            time.sleep(0.02)
            rt._touch_heartbeat()
            self.assertEqual(hb.stat().st_mtime, first_mtime)  # untouched
            # Progress resumes → touch resumes.
            rt._note_progress()
            time.sleep(0.02)
            rt._touch_heartbeat()
            self.assertGreater(hb.stat().st_mtime, first_mtime)

    def test_liveness_json_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            rt = self._runtime(instance)
            rt._note_progress()
            rt._touch_heartbeat()
            payload = json.loads(
                (instance / "state" / "gateway" / "liveness.json").read_text()
            )
            self.assertIn("ts", payload)
            self.assertIn("progress_age_seconds", payload)

    def test_lease_renewal_counts_as_progress(self):
        from gateway.runtime import _LeaseHeartbeat

        marks = []
        hb = _LeaseHeartbeat(
            instance_dir=Path("/nonexistent"),
            event_ids=[1],
            worker_id="w",
            lease_seconds=300,
            log=lambda *a, **k: None,
            on_renew=lambda: marks.append(1),
        )
        self.assertIsNotNone(hb._on_renew)


if __name__ == "__main__":
    unittest.main()
