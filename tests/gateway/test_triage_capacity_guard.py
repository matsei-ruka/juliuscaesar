"""Triage capacity guard — override Sonnet/Haiku to Opus when context heavy.

Sonnet and Haiku top out at 200K input. When the conversation already carries
a tracked session above the safe threshold, triage must not pick them — the
guard forces `claude:opus` so the 1M extended profile can absorb the turn.

Runtime model strings use the short alias form produced by the router
("sonnet-4-6" from a "claude:sonnet-4-6" brain spec, "haiku-4-5", etc.),
NOT the full canonical profile model id ("claude-sonnet-4-6"). Tests here
use the actual runtime values so they catch real dispatching bugs.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue  # noqa: E402
from gateway.config import render_default_config  # noqa: E402
from gateway.lifecycle import telemetry  # noqa: E402
from gateway.runtime import GatewayRuntime  # noqa: E402


def _make_instance() -> Path:
    root = Path(tempfile.mkdtemp(prefix="jc-triage-guard-"))
    (root / ".jc").write_text("", encoding="utf-8")
    (root / "ops").mkdir()
    (root / "memory" / "L1").mkdir(parents=True)
    (root / "memory" / "L1" / "IDENTITY.md").write_text("Test", encoding="utf-8")
    (root / "ops" / "gateway.yaml").write_text(
        render_default_config(default_brain="claude:sonnet-4-6"), encoding="utf-8"
    )
    return root


def _event(conversation_id: str = "c1") -> queue.Event:
    return queue.Event(
        id=1,
        source="telegram",
        source_message_id="m1",
        user_id="u1",
        conversation_id=conversation_id,
        content="hi",
        meta=None,
        status="running",
        received_at="2026-06-07T00:00:00Z",
        available_at="2026-06-07T00:00:00Z",
        locked_by="w1",
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


def _seed_usage(instance: Path, *, brain: str, tokens: int) -> None:
    conn = queue.connect(instance)
    try:
        telemetry.record_usage(
            conn,
            owner_key=f"gateway:telegram:c1:{brain}:0",
            brain=brain,
            usage=telemetry.ContextUsage.from_anthropic_usage(
                {
                    "input_tokens": tokens,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
                source="api",
            ),
            model="claude-sonnet-4-6",
            context_profile="claude-sonnet-4-6-standard",
        )
    finally:
        conn.close()


class TriageCapacityGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.instance = _make_instance()
        self.runtime = GatewayRuntime(
            self.instance,
            log_path=queue.queue_dir(self.instance) / "test.log",
            stop_requested=lambda: True,
        )

    def tearDown(self) -> None:
        self.runtime.close()

    def test_below_threshold_keeps_sonnet(self) -> None:
        _seed_usage(self.instance, brain="claude", tokens=120_000)
        # Runtime produces brain="claude", model="sonnet-4-6" from "claude:sonnet-4-6" spec.
        brain, model = self.runtime._triage_capacity_guard(
            event=_event(), channel="telegram", brain="claude", model="sonnet-4-6"
        )
        self.assertEqual(brain, "claude")
        self.assertEqual(model, "sonnet-4-6")

    def test_above_threshold_overrides_to_opus(self) -> None:
        _seed_usage(self.instance, brain="claude", tokens=180_000)
        brain, model = self.runtime._triage_capacity_guard(
            event=_event(), channel="telegram", brain="claude", model="sonnet-4-6"
        )
        # Guard splits "claude:opus" → brain="claude", model="opus".
        # invoke_brain("claude", ...) hits _BRAIN_REGISTRY["claude"] ✓.
        self.assertEqual(brain, "claude")
        self.assertEqual(model, "opus")

    def test_haiku_also_overrides(self) -> None:
        _seed_usage(self.instance, brain="claude", tokens=200_000)
        brain, model = self.runtime._triage_capacity_guard(
            event=_event(), channel="telegram", brain="claude", model="haiku-4-5"
        )
        self.assertEqual(brain, "claude")
        self.assertEqual(model, "opus")

    def test_opus_never_overridden(self) -> None:
        _seed_usage(self.instance, brain="claude", tokens=300_000)
        # "opus-4-7-1m" comes from the "opus" alias → "claude:opus-4-7-1m" spec.
        brain, model = self.runtime._triage_capacity_guard(
            event=_event(), channel="telegram", brain="claude", model="opus-4-7-1m"
        )
        self.assertEqual(brain, "claude")
        self.assertEqual(model, "opus-4-7-1m")

    def test_no_conversation_id_skips_guard(self) -> None:
        _seed_usage(self.instance, brain="claude", tokens=500_000)
        event = _event(conversation_id="")
        brain, model = self.runtime._triage_capacity_guard(
            event=event, channel="telegram", brain="claude", model="sonnet-4-6"
        )
        self.assertEqual(brain, "claude")
        self.assertEqual(model, "sonnet-4-6")

    def test_empty_telemetry_keeps_sonnet(self) -> None:
        # No usage recorded — table empty, guard is a no-op.
        brain, model = self.runtime._triage_capacity_guard(
            event=_event(), channel="telegram", brain="claude", model="sonnet-4-6"
        )
        self.assertEqual(brain, "claude")
        self.assertEqual(model, "sonnet-4-6")

    def test_model_none_skips_guard(self) -> None:
        # model=None means brain picked without a model spec; guard cannot
        # determine family, so it is a no-op regardless of context size.
        _seed_usage(self.instance, brain="claude", tokens=500_000)
        brain, model = self.runtime._triage_capacity_guard(
            event=_event(), channel="telegram", brain="claude", model=None
        )
        self.assertEqual(brain, "claude")
        self.assertIsNone(model)


if __name__ == "__main__":
    unittest.main()
