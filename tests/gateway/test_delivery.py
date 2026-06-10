from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.delivery import DeliveryAmbiguous, deliver_response  # noqa: E402


class DeliveryTests(unittest.TestCase):
    def test_live_channel_used_before_stateless_fallback(self):
        class FakeChannel:
            name = "discord"

            def send(self, response, meta):
                self.seen = (response, meta)
                return "m1"

        channel = FakeChannel()
        logs = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "gateway.delivery.deliver",
            side_effect=AssertionError("stateless fallback used"),
        ):
            message_id = deliver_response(
                instance_dir=Path(tmp),
                source="discord",
                response="pong",
                meta={"channel_id": "123"},
                config_channels={},
                live_channels={"discord": channel},
                log=lambda msg, **fields: logs.append((msg, fields)),
            )
        self.assertEqual(message_id, "m1")
        self.assertEqual(channel.seen[0], "pong")
        self.assertEqual(logs, [])

    def test_discord_without_live_channel_fails_hard(self):
        logs = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "gateway.delivery.deliver",
            side_effect=AssertionError("stateless fallback used"),
        ):
            message_id = deliver_response(
                instance_dir=Path(tmp),
                source="discord",
                response="pong",
                meta={"channel_id": "123"},
                config_channels={},
                live_channels={},
                log=lambda msg, **fields: logs.append((msg, fields)),
            )
        self.assertIsNone(message_id)
        self.assertTrue(any("no_live_channel" in msg for msg, _fields in logs))


class StrictIdempotencyTests(unittest.TestCase):
    """Audit Phase 2: the live-exception → stateless-fallback double-send.

    A live send that raises AFTER Telegram may have accepted the request
    (read timeout) must not be retried by the stateless fallback under
    `strict_idempotency`. Provably pre-delivery failures (connection
    refused, DNS) still fall back. Legacy callers keep always-fallback.
    """

    def _run(self, exc, *, strict):
        class FakeChannel:
            name = "telegram"

            def send(self, response, meta):
                raise exc

        fallback_calls = []

        def fake_deliver(**kwargs):
            fallback_calls.append(kwargs)
            return "fallback-id"

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "gateway.delivery.deliver", side_effect=fake_deliver
        ):
            message_id = deliver_response(
                instance_dir=Path(tmp),
                source="telegram",
                response="pong",
                meta={"chat_id": "1"},
                config_channels={},
                live_channels={"telegram": FakeChannel()},
                log=lambda msg, **fields: None,
                strict_idempotency=strict,
            )
        return message_id, fallback_calls

    def test_strict_timeout_raises_ambiguous_no_fallback(self):
        with self.assertRaises(DeliveryAmbiguous):
            self._run(TimeoutError("read timed out"), strict=True)

    def test_strict_unknown_exception_raises_ambiguous(self):
        with self.assertRaises(DeliveryAmbiguous):
            self._run(RuntimeError("boom"), strict=True)

    def test_strict_connection_refused_falls_back(self):
        message_id, fallback_calls = self._run(
            ConnectionRefusedError("refused"), strict=True
        )
        self.assertEqual(message_id, "fallback-id")
        self.assertEqual(len(fallback_calls), 1)

    def test_legacy_timeout_still_falls_back(self):
        message_id, fallback_calls = self._run(
            TimeoutError("read timed out"), strict=False
        )
        self.assertEqual(message_id, "fallback-id")
        self.assertEqual(len(fallback_calls), 1)


if __name__ == "__main__":
    unittest.main()
