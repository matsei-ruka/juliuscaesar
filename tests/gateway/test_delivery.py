from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.delivery import deliver_response  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
