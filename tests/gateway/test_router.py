"""Tests for lib/gateway/router.py — pure routing decisions."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.config import ChannelConfig, GatewayConfig  # noqa: E402
from gateway.queue import Event  # noqa: E402
from gateway import router  # noqa: E402


def make_event(
    *,
    source: str = "telegram",
    meta: dict | None = None,
    content: str = "hello",
    conversation_id: str | None = "c1",
) -> Event:
    return Event(
        id=1,
        source=source,
        source_message_id="m1",
        user_id="u1",
        conversation_id=conversation_id,
        content=content,
        meta=json.dumps(meta) if meta else None,
        status="queued",
        received_at="2026-04-25T00:00:00Z",
        available_at="2026-04-25T00:00:00Z",
        locked_by=None,
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


def make_cfg(default_brain: str = "claude", per_channel: dict | None = None) -> GatewayConfig:
    channels: dict[str, ChannelConfig] = {
        "telegram": ChannelConfig(enabled=True),
        "slack": ChannelConfig(enabled=False),
    }
    for name, brain in (per_channel or {}).items():
        channels[name] = ChannelConfig(enabled=True, brain=brain)
    return GatewayConfig(default_brain=default_brain, channels=channels)


class RouterTests(unittest.TestCase):
    def test_brain_override_wins(self):
        event = make_event(meta={"brain_override": "claude:opus-4-7-1m"})
        sel = router.route(event, cfg=make_cfg())
        self.assertEqual(sel.brain, "claude")
        self.assertEqual(sel.model, "opus-4-7-1m")
        self.assertEqual(sel.reason, "brain_override")

    def test_override_beats_sticky_and_triage(self):
        event = make_event(meta={"brain_override": "codex:gpt-5"})
        sticky = router.StickyHint(brain="claude")
        triage = router.TriageHint(brain="claude:haiku-4-5", model="haiku-4-5", confidence=0.99)
        sel = router.route(event, cfg=make_cfg(), sticky=sticky, triage=triage)
        self.assertEqual(sel.brain, "codex")
        self.assertEqual(sel.model, "gpt-5")
        self.assertEqual(sel.reason, "brain_override")

    def test_cron_pinned_brain(self):
        event = make_event(source="cron", meta={"brain": "codex:gpt-5", "task_name": "morning"})
        sel = router.route(event, cfg=make_cfg())
        self.assertEqual(sel.brain, "codex")
        self.assertEqual(sel.model, "gpt-5")
        self.assertEqual(sel.reason, "cron_pinned")

    def test_sticky_used_when_no_override(self):
        event = make_event()
        sticky = router.StickyHint(brain="codex", model="gpt-5")
        sel = router.route(event, cfg=make_cfg(), sticky=sticky)
        self.assertEqual(sel.brain, "codex")
        self.assertEqual(sel.model, "gpt-5")
        self.assertEqual(sel.reason, "sticky")

    def test_triage_used_when_confident(self):
        event = make_event()
        triage = router.TriageHint(brain="claude", model="opus-4-7-1m", confidence=0.9)
        sel = router.route(event, cfg=make_cfg(), triage=triage, confidence_threshold=0.7)
        self.assertEqual(sel.brain, "claude")
        self.assertEqual(sel.model, "opus-4-7-1m")
        self.assertEqual(sel.reason, "triage")

    def test_triage_low_confidence_falls_back(self):
        event = make_event()
        triage = router.TriageHint(brain="codex", model=None, confidence=0.3)
        sel = router.route(
            event,
            cfg=make_cfg(),
            triage=triage,
            confidence_threshold=0.7,
            fallback_brain="claude:sonnet-4-6",
        )
        self.assertEqual(sel.brain, "claude")
        self.assertEqual(sel.model, "sonnet-4-6")
        self.assertEqual(sel.reason, "triage_fallback")

    def test_channel_default_when_nothing_else_applies(self):
        event = make_event(source="telegram")
        cfg = make_cfg(default_brain="codex", per_channel={"telegram": "claude"})
        sel = router.route(event, cfg=cfg)
        self.assertEqual(sel.brain, "claude")
        self.assertEqual(sel.reason, "channel_default")

    def test_falls_back_to_global_default(self):
        event = make_event(source="telegram")
        cfg = make_cfg(default_brain="codex")
        sel = router.route(event, cfg=cfg)
        self.assertEqual(sel.brain, "codex")
        self.assertEqual(sel.reason, "channel_default")

    def test_channel_name_resolves_meta_channel(self):
        event = make_event(source="custom", meta={"channel": "discord"})
        self.assertEqual(router.channel_name(event), "discord")

    def test_channel_name_keeps_known_sources(self):
        self.assertEqual(router.channel_name(make_event(source="telegram")), "telegram")
        self.assertEqual(router.channel_name(make_event(source="slack")), "slack")


if __name__ == "__main__":
    unittest.main()
