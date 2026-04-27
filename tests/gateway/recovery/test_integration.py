from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue  # noqa: E402
from gateway.brains import AdapterFailure  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402
from gateway.recovery import Fail, Retry  # noqa: E402
from gateway.recovery_integration import RecoveryIntegration  # noqa: E402


class _Runtime:
    def __init__(self, instance_dir: Path):
        self.instance_dir = instance_dir
        self.config = GatewayConfig(max_retries=3)
        self.logs: list[str] = []
        self.deliveries: list[tuple[str, str, dict]] = []

    def log(self, message: str, **_fields):
        self.logs.append(message)

    def _deliver_response(self, source, response, meta):
        self.deliveries.append((source, response, meta))
        return "m1"


class _RetryDispatcher:
    def handle(self, event, failure):
        return Retry(reason="network", delay_seconds=1)

    def maybe_consume_auth_token(self, event):
        return False


class _FailDispatcher:
    def handle(self, event, failure):
        return Fail(reason="root/sudo permissions error")

    def maybe_consume_auth_token(self, event):
        return False


class RecoveryIntegrationTests(unittest.TestCase):
    def _event(self, instance: Path):
        conn = queue.connect(instance)
        try:
            event, _ = queue.enqueue(conn, source="manual", content="hi")
            claimed = queue.claim_next(conn, worker_id="w1", lease_seconds=30)
            self.assertEqual(claimed.id, event.id)
            return claimed
        finally:
            conn.close()

    def test_retry_decision_requeues_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            runtime = _Runtime(instance)
            integration = RecoveryIntegration.__new__(RecoveryIntegration)
            integration.runtime = runtime
            integration.dispatcher = _RetryDispatcher()
            event = self._event(instance)

            integration.handle_adapter_failure(
                event,
                AdapterFailure("claude", 1, "connection reset"),
            )

            conn = queue.connect(instance)
            try:
                saved = queue.get(conn, event.id)
            finally:
                conn.close()
            self.assertEqual(saved.status, "queued")
            self.assertEqual(saved.retry_count, 1)
            self.assertIn("recovery: retry", saved.error)

    def test_no_dispatcher_falls_back_to_blind_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            runtime = _Runtime(instance)
            integration = RecoveryIntegration.__new__(RecoveryIntegration)
            integration.runtime = runtime
            integration.dispatcher = None
            event = self._event(instance)

            integration.handle_adapter_failure(
                event,
                AdapterFailure("claude", 1, "plain failure"),
            )

            conn = queue.connect(instance)
            try:
                saved = queue.get(conn, event.id)
            finally:
                conn.close()
            self.assertEqual(saved.status, "queued")
            self.assertIn("adapter claude failed", saved.error)

    def test_fail_decision_notifies_original_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            runtime = _Runtime(instance)
            integration = RecoveryIntegration.__new__(RecoveryIntegration)
            integration.runtime = runtime
            integration.dispatcher = _FailDispatcher()
            event = self._event(instance)

            integration.handle_adapter_failure(
                event,
                AdapterFailure("claude", 1, "root/sudo privileges denied"),
            )

            conn = queue.connect(instance)
            try:
                saved = queue.get(conn, event.id)
            finally:
                conn.close()
            self.assertEqual(saved.status, "failed")
            self.assertEqual(len(runtime.deliveries), 1)
            source, response, meta = runtime.deliveries[0]
            self.assertEqual(source, "manual")
            self.assertEqual(meta["delivery_channel"], "manual")
            self.assertIn("Gateway adapter failed", response)
            self.assertIn("root/sudo permissions error", response)
            self.assertIn("root/sudo privileges denied", response)


if __name__ == "__main__":
    unittest.main()
