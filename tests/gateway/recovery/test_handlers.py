"""Recovery handlers — Retry / Fail / Defer routing."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue, sessions  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402
from gateway.queue import Event  # noqa: E402
from gateway.recovery.classifier import Classification  # noqa: E402
from gateway.recovery.handlers.bad_input import BadInputHandler  # noqa: E402
from gateway.recovery.handlers.base import (  # noqa: E402
    Defer,
    Fail,
    RecoveryContext,
    Retry,
)
from gateway.recovery.handlers.session_expired import SessionExpiredHandler  # noqa: E402
from gateway.recovery.handlers.session_missing import SessionMissingHandler  # noqa: E402
from gateway.recovery.handlers.transient import TransientHandler  # noqa: E402
from gateway.recovery.handlers.unknown import UnknownHandler  # noqa: E402


def _ctx(instance_dir: Path, *, env: dict[str, str] | None = None) -> RecoveryContext:
    config = GatewayConfig()
    return RecoveryContext(
        instance_dir=instance_dir,
        config=config,
        runtime=mock.Mock(),
        log=lambda msg, **fields: None,
    )


def _event(*, source: str = "telegram", conv: str | None = "c1", meta: dict | None = None,
           content: str = "hi", retry: int = 0) -> Event:
    return Event(
        id=42,
        source=source,
        source_message_id="m1",
        user_id="u1",
        conversation_id=conv,
        content=content,
        meta=json.dumps(meta) if meta else None,
        status="running",
        received_at="2026-04-26T00:00:00Z",
        available_at="2026-04-26T00:00:00Z",
        locked_by="w1",
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=retry,
        response=None,
        error=None,
    )


def _instance(tmp: str) -> Path:
    inst = Path(tmp)
    (inst / ".jc").write_text("", encoding="utf-8")
    return inst


class TransientHandlerTests(unittest.TestCase):
    def test_returns_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(_instance(tmp))
            decision = TransientHandler().handle(
                _event(),
                Classification(kind="transient", confidence=0.9),
                ctx,
            )
        self.assertIsInstance(decision, Retry)
        self.assertGreater(decision.delay_seconds, 0)


class BadInputHandlerTests(unittest.TestCase):
    def test_returns_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(_instance(tmp))
            decision = BadInputHandler().handle(
                _event(),
                Classification(kind="bad_input", confidence=0.95, extracted={"reason": "image too big"}),
                ctx,
            )
        self.assertIsInstance(decision, Fail)
        self.assertIn("image too big", decision.reason)


class UnknownHandlerTests(unittest.TestCase):
    def test_first_attempt_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(_instance(tmp))
            decision = UnknownHandler().handle(
                _event(retry=0),
                Classification(kind="unknown", confidence=0.0),
                ctx,
            )
        self.assertIsInstance(decision, Retry)

    def test_second_attempt_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(_instance(tmp))
            decision = UnknownHandler().handle(
                _event(retry=2),
                Classification(kind="unknown", confidence=0.0),
                ctx,
            )
        self.assertIsInstance(decision, Fail)


class SessionMissingHandlerTests(unittest.TestCase):
    def test_clears_sticky_and_redispatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = _instance(tmp)
            ctx = _ctx(inst)
            # Pre-seed a sticky session for the conversation.
            conn = queue.connect(inst)
            try:
                sessions.upsert_session(
                    conn,
                    channel="telegram",
                    conversation_id="c1",
                    brain="claude",
                    session_id="dead-uuid",
                )
                # Pre-seed the original event row so the handler's
                # `queue.fail(event_id)` can find it.
                queue.enqueue(
                    conn,
                    source="telegram",
                    source_message_id="m1",
                    user_id="u1",
                    conversation_id="c1",
                    content="hi",
                )
            finally:
                conn.close()

            decision = SessionMissingHandler().handle(
                _event(),
                Classification(
                    kind="session_missing",
                    confidence=0.97,
                    extracted={"session_id": "dead-uuid"},
                ),
                ctx,
            )
            self.assertIsInstance(decision, Defer)

            # Sticky should be cleared.
            conn = queue.connect(inst)
            try:
                row = sessions.get_session(
                    conn,
                    channel="telegram",
                    conversation_id="c1",
                    brain="claude",
                )
                self.assertIsNone(row)
                # A redispatch event should now exist with the meta flag.
                redis = conn.execute(
                    "SELECT meta FROM events WHERE source_message_id LIKE 'recovery:session_missing:%'"
                ).fetchone()
                self.assertIsNotNone(redis)
                meta = json.loads(redis["meta"])
                self.assertTrue(meta.get("session_missing_redispatch"))
            finally:
                conn.close()

    def test_already_redispatched_fails_hard(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(_instance(tmp))
            decision = SessionMissingHandler().handle(
                _event(meta={"session_missing_redispatch": True}),
                Classification(
                    kind="session_missing",
                    confidence=0.97,
                    extracted={"session_id": "dead-uuid"},
                ),
                ctx,
            )
        self.assertIsInstance(decision, Fail)
        self.assertEqual(decision.reason, "session_missing_recovery_failed")


class SessionExpiredHandlerTests(unittest.TestCase):
    def _config_with_operator(self, operator: str) -> RecoveryContext:
        with tempfile.TemporaryDirectory() as tmp:
            inst = _instance(tmp)
            (inst / ".env").write_text(
                f"TELEGRAM_CHAT_ID={operator}\nTELEGRAM_BOT_TOKEN=x\n",
                encoding="utf-8",
            )
            return _ctx(inst), inst

    def test_inserts_auth_pending_row_and_defers(self):
        ctx, inst = self._config_with_operator("12345")
        with mock.patch.object(SessionExpiredHandler, "_send_operator_dm"):
            decision = SessionExpiredHandler().handle(
                _event(meta={"chat_type": "private"}),
                Classification(
                    kind="session_expired",
                    confidence=0.95,
                    extracted={"login_url": "https://claude.ai/cli/auth?token=x"},
                ),
                ctx,
            )
        self.assertIsInstance(decision, Defer)
        conn = queue.connect(inst)
        try:
            row = conn.execute("SELECT * FROM auth_pending").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "waiting")
        finally:
            conn.close()

    def test_group_chat_fails_with_distinct_reason(self):
        ctx, _ = self._config_with_operator("12345")
        decision = SessionExpiredHandler().handle(
            _event(meta={"chat_type": "supergroup"}),
            Classification(
                kind="session_expired",
                confidence=0.95,
                extracted={},
            ),
            ctx,
        )
        self.assertIsInstance(decision, Fail)
        self.assertEqual(decision.reason, "auth_required_in_group")

    def test_second_failure_appends_to_pending_events(self):
        ctx, inst = self._config_with_operator("12345")
        with mock.patch.object(SessionExpiredHandler, "_send_operator_dm"):
            SessionExpiredHandler().handle(
                _event(meta={"chat_type": "private"}),
                Classification(kind="session_expired", confidence=0.9, extracted={}),
                ctx,
            )
            # Second event for same operator → existing row stays, event_id appended.
            second = Event(
                id=99, source="telegram", source_message_id="m2",
                user_id="u1", conversation_id="c1", content="hi",
                meta='{"chat_type":"private"}', status="running",
                received_at="2026-04-26T00:00:00Z", available_at="2026-04-26T00:00:00Z",
                locked_by="w1", locked_until=None, started_at=None, finished_at=None,
                retry_count=0, response=None, error=None,
            )
            decision = SessionExpiredHandler().handle(
                second,
                Classification(kind="session_expired", confidence=0.9, extracted={}),
                ctx,
            )
        self.assertIsInstance(decision, Defer)
        self.assertIn("appended", decision.reason)
        conn = queue.connect(inst)
        try:
            row = conn.execute("SELECT pending_events FROM auth_pending").fetchone()
            self.assertEqual(json.loads(row["pending_events"]), [99])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
