"""Tests for Phase 1 of supervisor card actions (`gateway.actions.stop_session`).

Covers:
  - Happy path: live subprocess SIGTERM'd within grace; registry marks stopped.
  - Double-tap idempotency: second call returns ``already_stopped=True``.
  - Unknown short_token: registry resolve returns None; telegram callback
    answers "session already ended" without calling ``stop_session``.
  - Unauthorized callback: chat_id not in the allowlist gets "not authorized"
    and no action runs.
  - Card keyboard: ``render_card(actions_short_token=...)`` attaches a
    two-button ``act:stop:<token>`` / ``act:bg:<token>`` inline keyboard.

Avoids any network I/O — Telegram HTTP is fully mocked.
"""

from __future__ import annotations

import os
import subprocess
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
from gateway.config import ChannelConfig  # noqa: E402
from supervisor.cards import Card, build_action_keyboard, render_card  # noqa: E402
from supervisor.models import PhaseResult  # noqa: E402


def _spawn_dummy_child() -> subprocess.Popen:
    """Spawn a long-sleeping subprocess in its own process group, like the brain adapter does."""
    return subprocess.Popen(
        ["sleep", "30"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _kill_if_alive(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def _make_instance(allowed_chat_ids: list[str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="actions-stop-"))
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


# ---------------------------------------------------------------------------
# actions.stop_session — happy path / idempotency / not-found
# ---------------------------------------------------------------------------


class StopSessionUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        actions_registry.clear()

    def tearDown(self) -> None:
        actions_registry.clear()

    def test_happy_path_terminates_child(self) -> None:
        proc = _spawn_dummy_child()
        try:
            session_id = "a" * 32
            actions_registry.register(
                short_token=session_id[:12],
                session_id=session_id,
                child_pid=proc.pid,
                slot_id=0,
                chat_id="12345",
            )
            result = actions.stop_session(session_id, stop_grace_seconds=2)
            self.assertTrue(result.ok, msg=f"reason={result.reason}")
            self.assertFalse(result.already_stopped)
            # Allow a brief grace for the subprocess to actually exit
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.fail("subprocess not terminated within grace window")
            self.assertIsNotNone(proc.returncode)
            self.assertNotEqual(proc.returncode, 0)
            entry = actions_registry.resolve_by_session(session_id)
            self.assertIsNotNone(entry)
            self.assertTrue(entry.stopped)
        finally:
            _kill_if_alive(proc)

    def test_double_tap_is_idempotent(self) -> None:
        proc = _spawn_dummy_child()
        try:
            session_id = "b" * 32
            actions_registry.register(
                short_token=session_id[:12],
                session_id=session_id,
                child_pid=proc.pid,
                slot_id=0,
            )
            first = actions.stop_session(session_id, stop_grace_seconds=2)
            second = actions.stop_session(session_id, stop_grace_seconds=2)
            self.assertTrue(first.ok)
            self.assertFalse(first.already_stopped)
            self.assertTrue(second.ok)
            self.assertTrue(second.already_stopped)
            self.assertEqual(second.reason, "already_stopped")
        finally:
            _kill_if_alive(proc)

    def test_unknown_session_returns_not_found(self) -> None:
        result = actions.stop_session("c" * 32)
        self.assertFalse(result.ok)
        self.assertFalse(result.already_stopped)
        self.assertEqual(result.reason, "not_found")
        self.assertEqual(result.elapsed_ms, 0)

    def test_pid_already_dead_returns_pid_gone(self) -> None:
        # Spawn + reap a child so its pid is gone.
        proc = subprocess.Popen(["true"], start_new_session=True)
        proc.wait()
        session_id = "d" * 32
        actions_registry.register(
            short_token=session_id[:12],
            session_id=session_id,
            child_pid=proc.pid,
            slot_id=0,
        )
        result = actions.stop_session(session_id, stop_grace_seconds=1)
        self.assertTrue(result.ok)
        self.assertTrue(result.already_stopped)
        self.assertEqual(result.reason, "pid_gone")


# ---------------------------------------------------------------------------
# Telegram callback routing — authorization + unknown token
# ---------------------------------------------------------------------------


class TelegramActionCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        actions_registry.clear()

    def tearDown(self) -> None:
        actions_registry.clear()
        gateway_config.clear_config_cache()
        gateway_config.clear_env_cache()

    def test_unauthorized_press_does_not_invoke_stop(self) -> None:
        instance = _make_instance(allowed_chat_ids=["28547271"])
        channel = _make_channel(instance, ["28547271"])
        try:
            session_id = "e" * 32
            actions_registry.register(
                short_token=session_id[:12],
                session_id=session_id,
                child_pid=999999,
                slot_id=0,
                chat_id="28547271",
            )
            update = {
                "callback_query": {
                    "id": "cq-1",
                    "data": f"act:stop:{session_id[:12]}",
                    "from": {"id": 999999},  # not in allowlist
                    "message": {
                        "message_id": 42,
                        "chat": {"id": 999999},
                        "text": "🛠️ activity",
                    },
                }
            }
            with patch("gateway.channels.telegram.http_json") as mock_http, \
                 patch("gateway.actions.stop_session") as mock_stop:
                mock_http.return_value = {"ok": True}
                channel._handle_callback_query(update)
                # _answer_callback should be the only HTTP call; no stop.
                self.assertFalse(mock_stop.called, "stop_session must NOT run for unauthorized press")
                # Verify the answer is "not authorized"
                self.assertTrue(mock_http.called)
                payload = mock_http.call_args.kwargs.get("data") or {}
                self.assertEqual(payload.get("text"), "not authorized")
        finally:
            channel.close()

    def test_unknown_short_token_answers_session_ended(self) -> None:
        instance = _make_instance(allowed_chat_ids=["28547271"])
        channel = _make_channel(instance, ["28547271"])
        try:
            update = {
                "callback_query": {
                    "id": "cq-2",
                    "data": "act:stop:nonexistent1",
                    "from": {"id": 28547271},  # authorized
                    "message": {
                        "message_id": 42,
                        "chat": {"id": 28547271},
                        "text": "🛠️ activity",
                    },
                }
            }
            with patch("gateway.channels.telegram.http_json") as mock_http, \
                 patch("gateway.actions.stop_session") as mock_stop:
                mock_http.return_value = {"ok": True}
                channel._handle_callback_query(update)
                self.assertFalse(mock_stop.called)
                payload = mock_http.call_args.kwargs.get("data") or {}
                self.assertEqual(payload.get("text"), "session already ended")
        finally:
            channel.close()

    def test_authorized_press_calls_stop_and_edits_card(self) -> None:
        instance = _make_instance(allowed_chat_ids=["28547271"])
        channel = _make_channel(instance, ["28547271"])
        try:
            session_id = "f" * 32
            actions_registry.register(
                short_token=session_id[:12],
                session_id=session_id,
                child_pid=999998,
                slot_id=0,
                chat_id="28547271",
            )
            update = {
                "callback_query": {
                    "id": "cq-3",
                    "data": f"act:stop:{session_id[:12]}",
                    "from": {"id": 28547271},
                    "message": {
                        "message_id": 101,
                        "chat": {"id": 28547271},
                        "text": "🛠️ scanning logs",
                    },
                }
            }
            stop_calls: list[str] = []

            def fake_stop(sid: str, *, stop_grace_seconds: int = 5, **_kw):
                stop_calls.append(sid)
                actions_registry.mark_stopped(sid)
                return actions.StopResult(
                    ok=True, already_stopped=False, elapsed_ms=12, reason="sigterm"
                )

            with patch("gateway.channels.telegram.http_json") as mock_http, \
                 patch("gateway.actions.stop_session", side_effect=fake_stop):
                mock_http.return_value = {"ok": True}
                channel._handle_callback_query(update)
                self.assertEqual(stop_calls, [session_id])
                # Two HTTP calls: answerCallbackQuery + editMessageText.
                urls = [c.args[0] for c in mock_http.call_args_list]
                self.assertTrue(any("answerCallbackQuery" in u for u in urls))
                self.assertTrue(any("editMessageText" in u for u in urls))
                # editMessageText call removes the keyboard.
                edit_call = next(
                    c for c in mock_http.call_args_list
                    if "editMessageText" in c.args[0]
                )
                edit_payload = edit_call.kwargs.get("data") or {}
                self.assertIn("reply_markup", edit_payload)
                self.assertIn("inline_keyboard", edit_payload["reply_markup"])
                # The new text appends the stop suffix.
                self.assertIn("Stopped", edit_payload.get("text", ""))
        finally:
            channel.close()

    def test_background_button_answers_coming_soon(self) -> None:
        instance = _make_instance(allowed_chat_ids=["28547271"])
        channel = _make_channel(instance, ["28547271"])
        try:
            update = {
                "callback_query": {
                    "id": "cq-bg",
                    "data": "act:bg:abc123def456",
                    "from": {"id": 28547271},
                    "message": {
                        "message_id": 200,
                        "chat": {"id": 28547271},
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
                self.assertEqual(payload.get("text"), "Coming soon")
        finally:
            channel.close()


# ---------------------------------------------------------------------------
# Card rendering with action keyboard
# ---------------------------------------------------------------------------


class CardKeyboardTests(unittest.TestCase):
    def test_render_card_omits_keyboard_by_default(self) -> None:
        phase = PhaseResult(phase="coding", emoji="🛠️", label={"en": "coding"})
        card = render_card(title="t", phase=phase, elapsed_seconds=5.0)
        self.assertIsNone(card.reply_markup)
        self.assertIsNone(card.short_token)

    def test_render_card_attaches_keyboard_when_token_set(self) -> None:
        phase = PhaseResult(phase="coding", emoji="🛠️", label={"en": "coding"})
        card = render_card(
            title="t",
            phase=phase,
            elapsed_seconds=5.0,
            actions_short_token="abc123def456",
        )
        self.assertIsInstance(card, Card)
        self.assertEqual(card.short_token, "abc123def456")
        self.assertIsNotNone(card.reply_markup)
        kb = card.reply_markup["inline_keyboard"]
        self.assertEqual(len(kb), 1)
        self.assertEqual(len(kb[0]), 2)
        self.assertEqual(kb[0][0]["callback_data"], "act:stop:abc123def456")
        self.assertEqual(kb[0][1]["callback_data"], "act:bg:abc123def456")

    def test_build_action_keyboard_shape(self) -> None:
        kb = build_action_keyboard("aaaa1111bbbb")
        self.assertEqual(
            kb,
            {
                "inline_keyboard": [[
                    {"text": "✋ Stop", "callback_data": "act:stop:aaaa1111bbbb"},
                    {"text": "🔄 Background", "callback_data": "act:bg:aaaa1111bbbb"},
                ]]
            },
        )


# ---------------------------------------------------------------------------
# Registry hygiene
# ---------------------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        actions_registry.clear()

    def tearDown(self) -> None:
        actions_registry.clear()

    def test_short_token_is_first_12_hex_chars(self) -> None:
        sid = "0123456789abcdef0123456789abcdef"
        self.assertEqual(actions_registry.short_token_for(sid), "0123456789ab")

    def test_resolve_by_event_returns_entry(self) -> None:
        sid = "1" * 32
        actions_registry.register(
            short_token=sid[:12],
            session_id=sid,
            child_pid=1234,
            slot_id=0,
            event_id=77,
        )
        entry = actions_registry.resolve_by_event(77)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.session_id, sid)

    def test_unregister_clears_all_indices(self) -> None:
        sid = "2" * 32
        actions_registry.register(
            short_token=sid[:12],
            session_id=sid,
            child_pid=1234,
            slot_id=0,
            event_id=88,
        )
        actions_registry.unregister(sid)
        self.assertIsNone(actions_registry.resolve_by_session(sid))
        self.assertIsNone(actions_registry.resolve(sid[:12]))
        self.assertIsNone(actions_registry.resolve_by_event(88))

    def test_attach_supervisor_message(self) -> None:
        sid = "3" * 32
        actions_registry.register(
            short_token=sid[:12],
            session_id=sid,
            child_pid=1234,
            slot_id=0,
        )
        ok = actions_registry.attach_supervisor_message_by_token(
            sid[:12], 555, card_text="hello card"
        )
        self.assertTrue(ok)
        entry = actions_registry.resolve(sid[:12])
        self.assertEqual(entry.supervisor_msg_id, 555)
        self.assertEqual(entry.card_text, "hello card")


if __name__ == "__main__":
    unittest.main()
