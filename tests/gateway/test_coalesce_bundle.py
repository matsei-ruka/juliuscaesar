"""Tests for GatewayRuntime._bundle_events.

Coalesce-mode regression coverage. The original implementation prefixed
each event with `[username]`, which collided with
`parse_inline_override` ('[spec] rest') and routed the synthetic event to
brain=username → retry loop. The fixed bundle uses `@username:` and tags
the synthetic event with `meta.coalesced_ids` so process_event can skip
slash + inline-override parsing entirely.
"""

from __future__ import annotations

import json
import unittest

from gateway import queue
from gateway.overrides import parse_inline_override, parse_slash_command
from gateway.runtime import GatewayRuntime


def _ev(
    *,
    eid: int,
    content: str,
    user_id: str = "u1",
    conversation_id: str = "c1",
    meta: dict | None = None,
) -> queue.Event:
    return queue.Event(
        id=eid,
        source="telegram",
        source_message_id=str(eid),
        user_id=user_id,
        conversation_id=conversation_id,
        content=content,
        meta=json.dumps(meta or {}, sort_keys=True, separators=(",", ":")),
        status="running",
        received_at="2026-05-16T00:00:00Z",
        available_at="2026-05-16T00:00:00Z",
        locked_by="w",
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


class BundleEventsTests(unittest.TestCase):
    def test_bundles_with_at_prefix_and_meta_flag(self) -> None:
        events = [
            _ev(eid=10, content="hi", user_id="u1", meta={"username": "alice"}),
            _ev(eid=11, content="how are you", user_id="u2", meta={"username": "bob"}),
            _ev(
                eid=12,
                content="any plan?",
                user_id="u1",
                meta={"username": "alice", "reply_to_message_id": 999},
            ),
        ]

        bundled = GatewayRuntime._bundle_events(events)

        self.assertEqual(bundled.id, 10)
        self.assertEqual(
            bundled.content,
            "@alice: hi\n\n@bob: how are you\n\n@alice: any plan?",
        )
        meta = json.loads(bundled.meta)
        self.assertEqual(meta["coalesced_ids"], [10, 11, 12])
        self.assertEqual(meta["reply_to_message_id"], 999)
        self.assertEqual(meta["username"], "alice")

    def test_bundle_content_does_not_trip_inline_override(self) -> None:
        events = [
            _ev(eid=20, content="hello", meta={"username": "carol"}),
            _ev(eid=21, content="[opus] please continue", meta={"username": "carol"}),
        ]

        bundled = GatewayRuntime._bundle_events(events)

        self.assertIsNone(parse_inline_override(bundled.content))
        self.assertIsNone(parse_slash_command(bundled.content))

    def test_username_falls_back_to_user_id_then_literal(self) -> None:
        events = [
            _ev(eid=30, content="x", user_id="42", meta={}),
            _ev(eid=31, content="y", user_id=None, meta={}),
        ]
        bundled = GatewayRuntime._bundle_events(events)
        self.assertEqual(bundled.content, "@42: x\n\n@user: y")


if __name__ == "__main__":
    unittest.main()
