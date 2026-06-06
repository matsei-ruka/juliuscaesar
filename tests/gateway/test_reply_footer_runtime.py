"""Runtime attachment points for the optional reply footer."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue, sessions, transcripts  # noqa: E402
from gateway.brain import BrainResult  # noqa: E402
from gateway.config import render_default_config  # noqa: E402
from gateway.runtime import GatewayRuntime  # noqa: E402


def _config_with_footer(enabled: bool = True) -> str:
    text = render_default_config(default_brain="claude:sonnet-4-6")
    return text.replace(
        "reply_footer:\n  enabled: false",
        f"reply_footer:\n  enabled: {str(enabled).lower()}",
    )


def _make_instance(*, footer_enabled: bool = True) -> Path:
    root = Path(tempfile.mkdtemp(prefix="jc-reply-footer-runtime-"))
    (root / ".jc").write_text("", encoding="utf-8")
    (root / "ops").mkdir()
    (root / "memory" / "L1").mkdir(parents=True)
    (root / "memory" / "L1" / "IDENTITY.md").write_text("Test", encoding="utf-8")
    (root / "ops" / "gateway.yaml").write_text(
        _config_with_footer(footer_enabled), encoding="utf-8"
    )
    return root


def _enqueue(instance: Path, *, meta: dict[str, object] | None = None) -> None:
    conn = queue.connect(instance)
    try:
        queue.enqueue(
            conn,
            source="telegram",
            source_message_id="m1",
            conversation_id="28547271",
            content="hi",
            meta=meta or {"chat_id": "28547271"},
        )
    finally:
        conn.close()


class ReplyFooterRuntimeTest(unittest.TestCase):
    def test_footer_is_delivered_and_recorded_for_text_reply(self) -> None:
        instance = _make_instance(footer_enabled=True)
        _enqueue(instance)
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        with mock.patch("gateway.runtime.time.monotonic", side_effect=[10.0, 14.2]), mock.patch(
            "gateway.runtime.invoke_brain",
            return_value=BrainResult("hello back", "sessionabcdef"),
        ), mock.patch("gateway.runtime.deliver_response", return_value="m-out") as deliver:
            self.assertTrue(runtime.dispatch_once())

        delivered = deliver.call_args.kwargs["response"]
        self.assertEqual(
            delivered,
            "hello back\n\n⚙️ claude:sonnet-4-6 · sess sessiona… · 4.2s",
        )
        events = list(transcripts.iter_events(transcripts.transcript_path(instance, "28547271")))
        self.assertEqual(events[-1].text, delivered)
        runtime.close()

    def test_footer_disabled_preserves_existing_reply(self) -> None:
        instance = _make_instance(footer_enabled=False)
        _enqueue(instance)
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        with mock.patch(
            "gateway.runtime.invoke_brain",
            return_value=BrainResult("hello back", "sessionabcdef"),
        ), mock.patch("gateway.runtime.deliver_response", return_value="m-out") as deliver:
            self.assertTrue(runtime.dispatch_once())
        self.assertEqual(deliver.call_args.kwargs["response"], "hello back")
        runtime.close()

    def test_voice_tts_gets_bare_text_but_channel_gets_footer(self) -> None:
        instance = _make_instance(footer_enabled=True)
        _enqueue(instance, meta={"chat_id": "28547271", "was_voice": True})
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        with mock.patch("gateway.runtime.time.monotonic", side_effect=[10.0, 11.0]), mock.patch(
            "gateway.runtime.invoke_brain",
            return_value=BrainResult("voice reply", "sess-voice"),
        ), mock.patch("gateway.runtime.deliver_response", return_value="m-out") as deliver, mock.patch.object(
            runtime, "_render_voice_reply"
        ) as render_voice:
            self.assertTrue(runtime.dispatch_once())
        render_voice.assert_called_once()
        voice_args = render_voice.call_args.args
        self.assertEqual(voice_args[0], "voice reply")
        self.assertEqual(voice_args[1]["chat_id"], "28547271")
        self.assertTrue(voice_args[1]["was_voice"])
        self.assertEqual(
            deliver.call_args.kwargs["response"],
            "voice reply\n\n⚙️ claude:sonnet-4-6 · sess sess-voi… · 1.0s",
        )
        runtime.close()

    def test_footer_falls_back_to_resumed_session_id_when_capture_returns_none(self) -> None:
        """Codex resume case: adapter returns session_id=None even though the
        dispatch ran inside a known session. Footer must show the resumed id,
        not "none".
        """
        instance = _make_instance(footer_enabled=True)
        _enqueue(instance)
        conn = queue.connect(instance)
        try:
            sessions.upsert_session(
                conn,
                channel="telegram",
                conversation_id="28547271",
                brain="claude",
                session_id="resumedabcdef1234",
                slot=0,
            )
        finally:
            conn.close()
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        with mock.patch("gateway.runtime.time.monotonic", side_effect=[10.0, 12.5]), mock.patch(
            "gateway.runtime.invoke_brain",
            return_value=BrainResult("hello back", None),
        ), mock.patch("gateway.runtime.deliver_response", return_value="m-out") as deliver:
            self.assertTrue(runtime.dispatch_once())
        delivered = deliver.call_args.kwargs["response"]
        self.assertIn("sess resumeda…", delivered)
        self.assertNotIn("sess none", delivered)
        runtime.close()

    def test_push_handled_path_is_not_footered_or_delivered(self) -> None:
        instance = _make_instance(footer_enabled=True)
        _enqueue(instance)
        runtime = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=lambda: True,
        )
        payload = json.dumps({"push_message_sent": True, "message": "already sent"})
        with mock.patch(
            "gateway.runtime.invoke_brain",
            return_value=BrainResult(payload, "sessionabcdef"),
        ), mock.patch("gateway.runtime.deliver_response", return_value="m-out") as deliver:
            self.assertTrue(runtime.dispatch_once())
        deliver.assert_not_called()
        events = list(transcripts.iter_events(transcripts.transcript_path(instance, "28547271")))
        self.assertEqual(events[-1].text, "already sent")
        runtime.close()


if __name__ == "__main__":
    unittest.main()
