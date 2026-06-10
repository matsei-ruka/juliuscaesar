"""Outbound idempotency ledger (audit Phase 2 #1/#2 — duplicate-reply root fix).

Delivery happens BEFORE `complete()`. A crash (or lease loss) in between
leaves the row `running`; the re-claim re-runs the brain and sends a second
reply — across process restarts, which the Phase 1 ownership gate cannot
see. The `deliveries` table is the durable delivered-marker: reserve before
sending, confirm after, skip when a prior claim already confirmed (or
attempted ambiguously).
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue  # noqa: E402
from gateway.delivery import DeliveryAmbiguous  # noqa: E402
from gateway.runtime import GatewayRuntime  # noqa: E402


def _instance() -> Path:
    root = Path(tempfile.mkdtemp(prefix="jc-ledger-"))
    (root / ".jc").write_text("", encoding="utf-8")
    return root


class LedgerQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _instance()
        self.conn = queue.connect(self.tmp)

    def tearDown(self) -> None:
        self.conn.close()

    def test_fresh_reservation_proceeds(self) -> None:
        verdict, msg_id = queue.begin_delivery(
            self.conn, event_id=1, channel="telegram", locked_by="w#aaa"
        )
        self.assertEqual(verdict, "proceed")
        self.assertIsNone(msg_id)
        row = queue.delivery_record(self.conn, event_id=1, channel="telegram")
        self.assertEqual(row["status"], "sending")
        self.assertEqual(row["locked_by"], "w#aaa")

    def test_confirmed_send_skips_future_claims(self) -> None:
        queue.begin_delivery(self.conn, event_id=1, channel="telegram", locked_by="w#aaa")
        queue.finish_delivery(self.conn, event_id=1, channel="telegram", message_id="m42")
        # A different claim (re-claim after crash-between-send-and-complete)
        # must NOT get to send again.
        verdict, msg_id = queue.begin_delivery(
            self.conn, event_id=1, channel="telegram", locked_by="w#bbb"
        )
        self.assertEqual(verdict, "already_sent")
        self.assertEqual(msg_id, "m42")

    def test_unconfirmed_attempt_from_other_claim_is_ambiguous(self) -> None:
        # Prior claim reserved, sent (maybe), and crashed before confirming.
        queue.begin_delivery(self.conn, event_id=1, channel="telegram", locked_by="w#aaa")
        verdict, _ = queue.begin_delivery(
            self.conn, event_id=1, channel="telegram", locked_by="w#bbb"
        )
        self.assertEqual(verdict, "ambiguous")

    def test_same_claim_reentry_proceeds(self) -> None:
        queue.begin_delivery(self.conn, event_id=1, channel="telegram", locked_by="w#aaa")
        verdict, _ = queue.begin_delivery(
            self.conn, event_id=1, channel="telegram", locked_by="w#aaa"
        )
        self.assertEqual(verdict, "proceed")

    def test_clear_releases_reservation_for_retry(self) -> None:
        queue.begin_delivery(self.conn, event_id=1, channel="telegram", locked_by="w#aaa")
        cleared = queue.clear_delivery(
            self.conn, event_id=1, channel="telegram", locked_by="w#aaa"
        )
        self.assertTrue(cleared)
        verdict, _ = queue.begin_delivery(
            self.conn, event_id=1, channel="telegram", locked_by="w#bbb"
        )
        self.assertEqual(verdict, "proceed")

    def test_clear_refuses_other_claims_and_sent_rows(self) -> None:
        queue.begin_delivery(self.conn, event_id=1, channel="telegram", locked_by="w#aaa")
        self.assertFalse(
            queue.clear_delivery(self.conn, event_id=1, channel="telegram", locked_by="w#bbb")
        )
        queue.finish_delivery(self.conn, event_id=1, channel="telegram", message_id="m1")
        self.assertFalse(
            queue.clear_delivery(self.conn, event_id=1, channel="telegram", locked_by="w#aaa")
        )

    def test_ledger_survives_reconnect(self) -> None:
        # The marker must outlive the process (crash-between-send-and-complete).
        queue.begin_delivery(self.conn, event_id=7, channel="telegram", locked_by="w#aaa")
        queue.finish_delivery(self.conn, event_id=7, channel="telegram", message_id="m7")
        self.conn.close()
        self.conn = queue.connect(self.tmp)
        verdict, msg_id = queue.begin_delivery(
            self.conn, event_id=7, channel="telegram", locked_by="w#new"
        )
        self.assertEqual(verdict, "already_sent")
        self.assertEqual(msg_id, "m7")

    def test_channels_are_independent_keys(self) -> None:
        queue.begin_delivery(self.conn, event_id=1, channel="telegram", locked_by="w#aaa")
        queue.finish_delivery(self.conn, event_id=1, channel="telegram", message_id="m1")
        verdict, _ = queue.begin_delivery(
            self.conn, event_id=1, channel="slack", locked_by="w#aaa"
        )
        self.assertEqual(verdict, "proceed")


class _Host:
    """Duck-typed runtime for `_deliver_response_idempotent`."""

    def __init__(self, instance_dir: Path) -> None:
        self.instance_dir = instance_dir
        self.logs: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.send_result: str | None = "m1"
        self.send_exc: Exception | None = None

    def log(self, message: str, **fields) -> None:
        self.logs.append(message)

    def _deliver_response(self, source, response, meta, *, strict_idempotency=False):
        if self.send_exc is not None:
            raise self.send_exc
        self.sent.append((source, response))
        return self.send_result


def _event(event_id: int, locked_by: str | None) -> queue.Event:
    return queue.Event(
        id=event_id,
        source="telegram",
        source_message_id=str(event_id),
        user_id="u1",
        conversation_id="c1",
        content="hi",
        meta=None,
        status="running",
        received_at="2026-06-10T00:00:00Z",
        available_at="2026-06-10T00:00:00Z",
        locked_by=locked_by,
        locked_until="2026-06-10T01:00:00Z",
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


class LedgerRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _instance()
        self.host = _Host(self.tmp)
        self.gated = GatewayRuntime._deliver_response_idempotent

    def test_successful_send_confirms_then_reclaim_skips(self) -> None:
        token_a = queue.mint_claim_token("gateway-1")
        out = self.gated(self.host, _event(5, token_a), "telegram", "hello", {})
        self.assertEqual(out, "m1")
        self.assertEqual(len(self.host.sent), 1)
        # Crash-between-send-and-complete: a fresh claim re-runs the brain
        # and tries to deliver again — must skip and reuse the prior id.
        token_b = queue.mint_claim_token("gateway-1")
        out2 = self.gated(self.host, _event(5, token_b), "telegram", "hello again", {})
        self.assertEqual(out2, "m1")
        self.assertEqual(len(self.host.sent), 1)
        self.assertTrue(any("already_sent" in line for line in self.host.logs))

    def test_provably_failed_send_clears_and_allows_retry(self) -> None:
        token_a = queue.mint_claim_token("gateway-1")
        self.host.send_result = None  # API-level definite no-send
        out = self.gated(self.host, _event(6, token_a), "telegram", "hello", {})
        self.assertIsNone(out)
        token_b = queue.mint_claim_token("gateway-1")
        self.host.send_result = "m2"
        out2 = self.gated(self.host, _event(6, token_b), "telegram", "hello", {})
        self.assertEqual(out2, "m2")
        self.assertEqual(len(self.host.sent), 2)

    def test_ambiguous_send_keeps_reservation_and_blocks_resend(self) -> None:
        token_a = queue.mint_claim_token("gateway-1")
        self.host.send_exc = DeliveryAmbiguous("read timeout post-accept")
        out = self.gated(self.host, _event(8, token_a), "telegram", "hello", {})
        self.assertIsNone(out)
        # Retry under a new claim must NOT resend: outcome unknown.
        self.host.send_exc = None
        token_b = queue.mint_claim_token("gateway-1")
        out2 = self.gated(self.host, _event(8, token_b), "telegram", "hello", {})
        self.assertIsNone(out2)
        self.assertEqual(self.host.sent, [])
        self.assertTrue(any("prior_attempt_unconfirmed" in line for line in self.host.logs))

    def test_unclaimed_events_bypass_ledger(self) -> None:
        # Direct calls / tests: locked_by is not a claim token → no ledger.
        out = self.gated(self.host, _event(9, None), "telegram", "hello", {})
        self.assertEqual(out, "m1")
        out2 = self.gated(self.host, _event(9, None), "telegram", "hello", {})
        self.assertEqual(out2, "m1")
        self.assertEqual(len(self.host.sent), 2)
        self.assertIsNone(
            queue.delivery_record(queue.connect(self.tmp), event_id=9, channel="telegram")
        )


if __name__ == "__main__":
    unittest.main()
