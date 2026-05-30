"""Tests for Phase 2 of supervisor card actions (`gateway.actions.background_session`).

Covers:
  - Happy path: backgrounded session marked + audit logged.
  - Double-tap idempotency (already_backgrounded=True).
  - Cap enforcement (refuse beyond max_background_per_chat).
  - Mid-task tool send suppressed + buffered; pass-through when flag off.
  - Routing bypass: backgrounded conversation forces fresh resume.
  - Unauthorized Background callback (no action runs).
  - Authorized Background callback edits card + replaces keyboard.
  - Completion card render appends "🔄 Done at HH:MM:SS UTC · MM:SS" and
    drops the keyboard.
  - Audit log entry written to state/actions.jsonl.

Avoids any network I/O — Telegram HTTP is fully mocked.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import actions, actions_registry  # noqa: E402
from gateway import config as gateway_config  # noqa: E402
from gateway import queue  # noqa: E402
from gateway.channels.telegram import TelegramChannel  # noqa: E402
from gateway.channels.telegram_outbound import send_text  # noqa: E402
from gateway.config import ChannelConfig  # noqa: E402


def _make_instance(allowed_chat_ids: list[str], *, max_bg: int = 3) -> Path:
    root = Path(tempfile.mkdtemp(prefix="actions-bg-"))
    (root / "ops").mkdir()
    chat_ids_list = ", ".join(allowed_chat_ids)
    (root / "ops" / "gateway.yaml").write_text(
        "default_brain: claude\n"
        "channels:\n"
        "  telegram:\n"
        "    enabled: true\n"
        "    token_env: TELEGRAM_BOT_TOKEN\n"
        f"    chat_ids: [{chat_ids_list}]\n"
        "actions:\n"
        "  enabled: true\n"
        "  stop_grace_seconds: 2\n"
        f"  max_background_per_chat: {max_bg}\n"
        "  suppress_background_tool_messages: true\n"
    )
    (root / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=test-token-xyz\n"
        f"TELEGRAM_CHAT_ID='{allowed_chat_ids[0]}'\n"
    )
    queue.connect(root)
    gateway_config.clear_config_cache()
    gateway_config.clear_env_cache()
    return root


def _make_channel(instance: Path, allowed_chat_ids: list[str]) -> TelegramChannel:
    cfg = ChannelConfig(
        enabled=True,
        chat_ids=tuple(allowed_chat_ids),
        token_env="TELEGRAM_BOT_TOKEN",
    )
    return TelegramChannel(instance_dir=instance, cfg=cfg, log=MagicMock())


def _register(session_id: str, *, chat_id: str = "28547271", pid: int = 999000) -> str:
    """Register an entry and return the short token."""
    token = actions_registry.short_token_for(session_id)
    actions_registry.register(
        short_token=token,
        session_id=session_id,
        child_pid=pid,
        slot_id=0,
        chat_id=chat_id,
        conversation_id=chat_id,
    )
    return token


# ---------------------------------------------------------------------------
# actions.background_session — happy path / idempotency / cap / audit
# ---------------------------------------------------------------------------


class BackgroundSessionUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        actions_registry.clear()

    def tearDown(self) -> None:
        actions_registry.clear()

    def test_happy_path_marks_backgrounded(self) -> None:
        sid = "a" * 32
        _register(sid)
        result = actions.background_session(
            sid, chat_id="28547271", supervisor_msg_id=42
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.already_backgrounded)
        self.assertFalse(result.capped)
        entry = actions_registry.resolve_by_session(sid)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.role, "backgrounded")
        self.assertEqual(entry.bg_supervisor_msg_id, 42)
        self.assertEqual(entry.bg_chat_id, "28547271")
        self.assertGreater(entry.backgrounded_at, 0.0)

    def test_double_tap_returns_already_backgrounded(self) -> None:
        sid = "b" * 32
        _register(sid)
        first = actions.background_session(
            sid, chat_id="28547271", supervisor_msg_id=1
        )
        second = actions.background_session(
            sid, chat_id="28547271", supervisor_msg_id=1
        )
        self.assertTrue(first.ok)
        self.assertFalse(first.already_backgrounded)
        self.assertTrue(second.ok)
        self.assertTrue(second.already_backgrounded)
        self.assertEqual(second.reason, "already_backgrounded")

    def test_unknown_session_returns_not_found(self) -> None:
        result = actions.background_session(
            "c" * 32, chat_id="28547271", supervisor_msg_id=1
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "not_found")

    def test_cap_enforced_per_chat(self) -> None:
        # Three sessions for the same chat → all background succeed.
        for n in range(3):
            sid = chr(ord("d") + n) * 32
            _register(sid, pid=900000 + n)
            res = actions.background_session(
                sid, chat_id="28547271", supervisor_msg_id=10 + n, max_per_chat=3
            )
            self.assertTrue(res.ok, msg=f"sess {n}: {res.reason}")
        # Fourth attempt for same chat must be refused (capped).
        sid4 = "z" * 32
        _register(sid4, pid=900003)
        res = actions.background_session(
            sid4, chat_id="28547271", supervisor_msg_id=99, max_per_chat=3
        )
        self.assertFalse(res.ok)
        self.assertTrue(res.capped)
        self.assertEqual(res.reason, "cap_reached")
        # The refused entry must remain primary (no demotion side effect).
        self.assertEqual(actions_registry.resolve_by_session(sid4).role, "primary")

    def test_audit_log_written(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="bg-audit-"))
        sid = "e" * 32
        _register(sid)
        actions.background_session(
            sid,
            chat_id="28547271",
            supervisor_msg_id=11,
            instance_dir=root,
        )
        log_path = root / "state" / "actions.jsonl"
        self.assertTrue(log_path.exists())
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["verb"], "background")
        self.assertEqual(record["session_id"], sid)
        self.assertEqual(record["chat_id"], "28547271")
        self.assertTrue(record["result"]["ok"])

    def test_get_primary_and_backgrounded_helpers(self) -> None:
        primary = "1" * 32
        bg1 = "2" * 32
        bg2 = "3" * 32
        _register(primary, pid=1)
        _register(bg1, pid=2)
        _register(bg2, pid=3)
        actions.background_session(bg1, chat_id="28547271", supervisor_msg_id=20)
        actions.background_session(bg2, chat_id="28547271", supervisor_msg_id=21)
        prim = actions_registry.get_primary("28547271")
        self.assertIsNotNone(prim)
        self.assertEqual(prim.session_id, primary)
        bg_entries = actions_registry.get_backgrounded_by_chat_id("28547271")
        self.assertEqual(
            {e.session_id for e in bg_entries}, {bg1, bg2}
        )
        self.assertEqual(actions_registry.count_backgrounded_for_chat("28547271"), 2)


# ---------------------------------------------------------------------------
# Mid-task telegram suppression
# ---------------------------------------------------------------------------


class TelegramOutboundSuppressionTests(unittest.TestCase):
    def setUp(self) -> None:
        actions_registry.clear()

    def tearDown(self) -> None:
        actions_registry.clear()

    def test_backgrounded_send_buffered_not_sent(self) -> None:
        sid = "f" * 32
        _register(sid)
        actions.background_session(sid, chat_id="28547271", supervisor_msg_id=1)
        instance = Path(tempfile.mkdtemp(prefix="bg-out-"))
        with patch(
            "gateway.channels.telegram_outbound.http_json"
        ) as mock_http:
            mock_http.return_value = {"ok": True, "result": {"message_id": 1}}
            out = send_text(
                instance_dir=instance,
                token="t",
                response="mid-task status update",
                meta={"chat_id": "28547271"},
                log=MagicMock(),
                action_session_id=sid,
            )
        self.assertIsNone(out)
        self.assertFalse(mock_http.called)
        entry = actions_registry.resolve_by_session(sid)
        self.assertEqual(
            entry.buffered_tool_messages, ["mid-task status update"]
        )

    def test_suppress_flag_off_passes_through(self) -> None:
        sid = "g" * 32
        _register(sid)
        actions.background_session(sid, chat_id="28547271", supervisor_msg_id=1)
        instance = Path(tempfile.mkdtemp(prefix="bg-out-flag-"))
        with patch(
            "gateway.channels.telegram_outbound.http_json"
        ) as mock_http:
            mock_http.return_value = {"ok": True, "result": {"message_id": 99}}
            out = send_text(
                instance_dir=instance,
                token="t",
                response="mid-task status update",
                meta={"chat_id": "28547271"},
                log=MagicMock(),
                action_session_id=sid,
                suppress_if_backgrounded=False,
            )
        self.assertEqual(out, "99")
        self.assertTrue(mock_http.called)
        entry = actions_registry.resolve_by_session(sid)
        self.assertEqual(entry.buffered_tool_messages, [])

    def test_primary_session_pass_through(self) -> None:
        sid = "h" * 32
        _register(sid)  # role stays primary
        instance = Path(tempfile.mkdtemp(prefix="bg-out-prim-"))
        with patch(
            "gateway.channels.telegram_outbound.http_json"
        ) as mock_http:
            mock_http.return_value = {"ok": True, "result": {"message_id": 7}}
            out = send_text(
                instance_dir=instance,
                token="t",
                response="hello",
                meta={"chat_id": "28547271"},
                log=MagicMock(),
                action_session_id=sid,
            )
        self.assertEqual(out, "7")
        self.assertTrue(mock_http.called)


# ---------------------------------------------------------------------------
# Telegram callback handler — auth, success, edit
# ---------------------------------------------------------------------------


class TelegramBackgroundCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        actions_registry.clear()

    def tearDown(self) -> None:
        actions_registry.clear()
        gateway_config.clear_config_cache()
        gateway_config.clear_env_cache()

    def test_unauthorized_press_does_not_invoke_background(self) -> None:
        instance = _make_instance(allowed_chat_ids=["28547271"])
        channel = _make_channel(instance, ["28547271"])
        try:
            sid = "i" * 32
            token = _register(sid)
            update = {
                "callback_query": {
                    "id": "cq-bg-noauth",
                    "data": f"act:bg:{token}",
                    "from": {"id": 999999},  # not in allowlist
                    "message": {
                        "message_id": 42,
                        "chat": {"id": 999999},
                        "text": "🛠️ running",
                    },
                }
            }
            with patch("gateway.channels.telegram.http_json") as mock_http, \
                 patch("gateway.actions.background_session") as mock_bg:
                mock_http.return_value = {"ok": True}
                channel._handle_callback_query(update)
                self.assertFalse(mock_bg.called)
                payload = mock_http.call_args.kwargs.get("data") or {}
                self.assertEqual(payload.get("text"), "not authorized")
        finally:
            channel.close()

    def test_authorized_press_backgrounds_and_edits_card(self) -> None:
        instance = _make_instance(allowed_chat_ids=["28547271"])
        channel = _make_channel(instance, ["28547271"])
        try:
            sid = "j" * 32
            token = _register(sid)
            # Supervisor message id is already bound from delivery layer:
            actions_registry.attach_supervisor_message_by_token(
                token, 101, card_text="🛠️ scanning logs"
            )
            update = {
                "callback_query": {
                    "id": "cq-bg-ok",
                    "data": f"act:bg:{token}",
                    "from": {"id": 28547271},
                    "message": {
                        "message_id": 101,
                        "chat": {"id": 28547271},
                        "text": "🛠️ scanning logs",
                    },
                }
            }
            with patch("gateway.channels.telegram.http_json") as mock_http:
                mock_http.return_value = {"ok": True}
                channel._handle_callback_query(update)
            urls = [c.args[0] for c in mock_http.call_args_list]
            self.assertTrue(any("answerCallbackQuery" in u for u in urls))
            self.assertTrue(any("editMessageText" in u for u in urls))
            answer_call = next(
                c for c in mock_http.call_args_list
                if "answerCallbackQuery" in c.args[0]
            )
            self.assertEqual(
                (answer_call.kwargs.get("data") or {}).get("text"), "Backgrounded"
            )
            edit_call = next(
                c for c in mock_http.call_args_list
                if "editMessageText" in c.args[0]
            )
            edit_payload = edit_call.kwargs.get("data") or {}
            self.assertIn("reply_markup", edit_payload)
            self.assertIn("Backgrounded", edit_payload.get("text", ""))
            # Single disabled button row replacing Stop/Background.
            keyboard = json.loads(edit_payload["reply_markup"])
            self.assertEqual(len(keyboard["inline_keyboard"]), 1)
            self.assertEqual(len(keyboard["inline_keyboard"][0]), 1)
            self.assertIn("Backgrounded", keyboard["inline_keyboard"][0][0]["text"])
            # Registry now reflects backgrounded role.
            entry = actions_registry.resolve_by_session(sid)
            self.assertEqual(entry.role, "backgrounded")
        finally:
            channel.close()

    def test_press_when_already_backgrounded_answers_already_done(self) -> None:
        instance = _make_instance(allowed_chat_ids=["28547271"])
        channel = _make_channel(instance, ["28547271"])
        try:
            sid = "k" * 32
            tok = _register(sid)
            actions.background_session(sid, chat_id="28547271", supervisor_msg_id=11)
            update = {
                "callback_query": {
                    "id": "cq-bg-dup",
                    "data": f"act:bg:{tok}",
                    "from": {"id": 28547271},
                    "message": {
                        "message_id": 11,
                        "chat": {"id": 28547271},
                        "text": "🛠️ running\n\n🔄 Backgrounded at 12:00:00 UTC",
                    },
                }
            }
            with patch("gateway.channels.telegram.http_json") as mock_http:
                mock_http.return_value = {"ok": True}
                channel._handle_callback_query(update)
            answer_call = next(
                c for c in mock_http.call_args_list
                if "answerCallbackQuery" in c.args[0]
            )
            self.assertEqual(
                (answer_call.kwargs.get("data") or {}).get("text"), "Already done"
            )
        finally:
            channel.close()

    def test_press_when_cap_reached_answers_limit(self) -> None:
        instance = _make_instance(allowed_chat_ids=["28547271"], max_bg=1)
        channel = _make_channel(instance, ["28547271"])
        try:
            # Fill the cap.
            sid_full = "l" * 32
            _register(sid_full, pid=900100)
            actions.background_session(
                sid_full, chat_id="28547271", supervisor_msg_id=20
            )
            # New press should be refused.
            sid_new = "m" * 32
            tok = _register(sid_new, pid=900101)
            update = {
                "callback_query": {
                    "id": "cq-bg-cap",
                    "data": f"act:bg:{tok}",
                    "from": {"id": 28547271},
                    "message": {
                        "message_id": 30,
                        "chat": {"id": 28547271},
                        "text": "🛠️ another",
                    },
                }
            }
            with patch("gateway.channels.telegram.http_json") as mock_http:
                mock_http.return_value = {"ok": True}
                channel._handle_callback_query(update)
            answer_call = next(
                c for c in mock_http.call_args_list
                if "answerCallbackQuery" in c.args[0]
            )
            self.assertEqual(
                (answer_call.kwargs.get("data") or {}).get("text"), "Limit reached"
            )
            self.assertEqual(
                actions_registry.resolve_by_session(sid_new).role, "primary"
            )
        finally:
            channel.close()


# ---------------------------------------------------------------------------
# Runtime routing + completion delivery
# ---------------------------------------------------------------------------


class RuntimeRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        actions_registry.clear()

    def tearDown(self) -> None:
        actions_registry.clear()

    def test_has_backgrounded_for_conversation_true_when_present(self) -> None:
        sid = "n" * 32
        token = actions_registry.short_token_for(sid)
        actions_registry.register(
            short_token=token,
            session_id=sid,
            child_pid=1,
            slot_id=0,
            chat_id="28547271",
            conversation_id="conv-xyz",
        )
        actions.background_session(sid, chat_id="28547271", supervisor_msg_id=1)
        self.assertTrue(
            actions_registry.has_backgrounded_for_conversation("conv-xyz")
        )
        self.assertFalse(
            actions_registry.has_backgrounded_for_conversation("conv-other")
        )

    def test_has_backgrounded_false_when_only_primary(self) -> None:
        sid = "o" * 32
        token = actions_registry.short_token_for(sid)
        actions_registry.register(
            short_token=token,
            session_id=sid,
            child_pid=1,
            slot_id=0,
            chat_id="28547271",
            conversation_id="conv-prim",
        )
        # Don't background — registry has only primary.
        self.assertFalse(
            actions_registry.has_backgrounded_for_conversation("conv-prim")
        )


class BackgroundCompletionRenderTests(unittest.TestCase):
    """Verify the runtime's `_handle_background_completion` formatting.

    Uses a minimal stand-in BrainResult + a fake runtime that records
    its outbound `send_text` and `editMessageText` calls. This isolates
    the formatting logic from queue/dispatch wiring.
    """

    def setUp(self) -> None:
        actions_registry.clear()

    def tearDown(self) -> None:
        actions_registry.clear()
        gateway_config.clear_config_cache()
        gateway_config.clear_env_cache()

    def test_completion_prepends_buffered_and_drops_keyboard(self) -> None:
        from gateway.brains.base import BrainResult
        from gateway.runtime import GatewayRuntime
        from gateway import queue as queue_module

        instance = _make_instance(allowed_chat_ids=["28547271"])
        runtime = GatewayRuntime(
            instance_dir=instance,
            log_path=instance / "state" / "gateway.log",
            stop_requested=lambda: True,
        )

        event = queue_module.Event(
            id=1, source="telegram", source_message_id=None,
            user_id="u", conversation_id="28547271",
            content="hello", meta=json.dumps({"chat_id": "28547271"}),
            status="running", received_at="2026-05-29T00:00:00Z",
            available_at="2026-05-29T00:00:00Z",
            locked_by="w", locked_until=None,
            started_at=None, finished_at=None,
            retry_count=0, response=None, error=None,
        )
        # Pretend the session started 65 seconds ago → MM:SS = 01:05.
        started = time.time() - 65.0
        result = BrainResult(
            response='{"push_message_sent": false, "message": "final body"}',
            session_id="brain-sess-1",
            action_session_id="ssid",
            action_role="backgrounded",
            action_bg_chat_id="28547271",
            action_bg_supervisor_msg_id=101,
            action_buffered_tool_messages=("partial 1", "partial 2"),
            action_started_at=started,
            action_card_text="🛠️ scanning logs",
        )
        with patch(
            "gateway.runtime.telegram_send_text"
        ) as mock_send, patch(
            "gateway.runtime.GatewayRuntime._edit_supervisor_card_after_background_done"
        ) as mock_edit:
            runtime._handle_background_completion(
                event=event,
                brain="claude",
                model=None,
                result=result,
                meta={"chat_id": "28547271"},
                channel="telegram",
                monotonic_start=time.monotonic(),
                slot=0,
            )
        self.assertTrue(mock_send.called)
        send_kwargs = mock_send.call_args.kwargs
        body = send_kwargs.get("response", "")
        self.assertTrue(
            body.startswith("🔄 Background done · "),
            msg=f"unexpected header: {body!r}",
        )
        # Buffered messages prepended before final body.
        self.assertIn("partial 1", body)
        self.assertIn("partial 2", body)
        self.assertIn("final body", body)
        self.assertLess(body.index("partial 1"), body.index("final body"))
        # Card edit called with the captured supervisor msg id.
        self.assertTrue(mock_edit.called)
        self.assertEqual(mock_edit.call_args.kwargs.get("message_id"), 101)
        self.assertIn("🔄 Done at", mock_edit.call_args.kwargs.get("text", ""))

    def test_completion_with_no_buffered_only_final_body(self) -> None:
        from gateway.brains.base import BrainResult
        from gateway.runtime import GatewayRuntime
        from gateway import queue as queue_module

        instance = _make_instance(allowed_chat_ids=["28547271"])
        runtime = GatewayRuntime(
            instance_dir=instance,
            log_path=instance / "state" / "gateway.log",
            stop_requested=lambda: True,
        )

        event = queue_module.Event(
            id=2, source="telegram", source_message_id=None,
            user_id="u", conversation_id="28547271",
            content="hi", meta=json.dumps({"chat_id": "28547271"}),
            status="running", received_at="2026-05-29T00:00:00Z",
            available_at="2026-05-29T00:00:00Z",
            locked_by="w", locked_until=None,
            started_at=None, finished_at=None,
            retry_count=0, response=None, error=None,
        )
        result = BrainResult(
            response='{"push_message_sent": false, "message": "done"}',
            session_id="brain-sess-2",
            action_session_id="ssid2",
            action_role="backgrounded",
            action_bg_chat_id="28547271",
            action_bg_supervisor_msg_id=202,
            action_buffered_tool_messages=(),
            action_started_at=time.time() - 5.0,
            action_card_text="🛠️ scanning",
        )
        with patch(
            "gateway.runtime.telegram_send_text"
        ) as mock_send, patch(
            "gateway.runtime.GatewayRuntime._edit_supervisor_card_after_background_done"
        ):
            runtime._handle_background_completion(
                event=event,
                brain="claude",
                model=None,
                result=result,
                meta={"chat_id": "28547271"},
                channel="telegram",
                monotonic_start=time.monotonic(),
                slot=0,
            )
        body = mock_send.call_args.kwargs.get("response", "")
        self.assertIn("🔄 Background done · ", body)
        self.assertIn("done", body)
        self.assertNotIn("partial", body)


if __name__ == "__main__":
    unittest.main()
