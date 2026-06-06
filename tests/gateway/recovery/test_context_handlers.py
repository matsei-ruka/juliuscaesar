"""§17.3 context-recovery handlers — rotate once, redispatch, never loop."""

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
from gateway.recovery.handlers.base import Fail, RecoveryContext, Retry  # noqa: E402
from gateway.recovery.handlers.context_exhausted import ContextExhaustedHandler  # noqa: E402
from gateway.recovery.handlers.context_profile_unavailable import (  # noqa: E402
    ContextProfileUnavailableHandler,
)


def _ctx(instance_dir: Path) -> RecoveryContext:
    runtime = mock.Mock()
    runtime.config = GatewayConfig()
    runtime.instance_dir = instance_dir
    return RecoveryContext(
        instance_dir=instance_dir,
        config=GatewayConfig(),
        runtime=runtime,
        log=lambda msg, **fields: None,
    )


def _event(*, conv: str | None = "c1", meta: dict | None = None) -> Event:
    return Event(
        id=42,
        source="telegram",
        source_message_id="m1",
        user_id="u1",
        conversation_id=conv,
        content="hi",
        meta=json.dumps(meta) if meta else None,
        status="running",
        received_at="2026-04-26T00:00:00Z",
        available_at="2026-04-26T00:00:00Z",
        locked_by="w1",
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


def _instance(tmp: str) -> Path:
    inst = Path(tmp)
    (inst / ".jc").write_text("", encoding="utf-8")
    conn = queue.connect(inst)
    try:
        sessions.upsert_session(
            conn,
            channel="telegram",
            conversation_id="c1",
            brain="claude",
            session_id="sess-aaaa",
            slot=0,
        )
    finally:
        conn.close()
    return inst


class ContextExhaustedHandlerTests(unittest.TestCase):
    def test_first_pass_rotates_notifies_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = _instance(tmp)
            ctx = _ctx(inst)
            with mock.patch(
                "gateway.recovery.handlers.context_exhausted.notify.notify_compaction"
            ) as notify_fn:
                decision = ContextExhaustedHandler().handle(
                    _event(),
                    Classification(kind="context_exhausted", confidence=0.9),
                    ctx,
                )
            self.assertIsInstance(decision, Retry)
            notify_fn.assert_called_once()
            # Slot mapping cleared by rotation.
            conn = queue.connect(inst)
            try:
                self.assertIsNone(
                    sessions.get_session(
                        conn, channel="telegram", conversation_id="c1", brain="claude", slot=0
                    )
                )
            finally:
                conn.close()

    def test_second_pass_with_marker_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = _instance(tmp)
            ctx = _ctx(inst)
            decision = ContextExhaustedHandler().handle(
                _event(meta={"context_rotation_recovery": True}),
                Classification(kind="context_exhausted", confidence=0.9),
                ctx,
            )
            self.assertIsInstance(decision, Fail)
            self.assertEqual(decision.reason, "context_exhausted_recovery_failed")


class ContextProfileUnavailableHandlerTests(unittest.TestCase):
    def test_first_pass_rotates_and_retries_without_notify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = _instance(tmp)
            ctx = _ctx(inst)
            decision = ContextProfileUnavailableHandler().handle(
                _event(),
                Classification(kind="context_profile_unavailable", confidence=0.9),
                ctx,
            )
            self.assertIsInstance(decision, Retry)

    def test_second_pass_with_marker_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = _instance(tmp)
            ctx = _ctx(inst)
            decision = ContextProfileUnavailableHandler().handle(
                _event(meta={"context_profile_recovery": True}),
                Classification(kind="context_profile_unavailable", confidence=0.9),
                ctx,
            )
            self.assertIsInstance(decision, Fail)


if __name__ == "__main__":
    unittest.main()
