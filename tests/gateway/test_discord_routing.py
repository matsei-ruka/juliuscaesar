"""Tests for Discord routing, auth, and interaction-id parsing (discord-parity).

The security-critical pieces — default-deny authorization, mention-gating, and
the button ``custom_id`` contract — are exercised here without a live
``discord.py`` event loop (it's an optional dep, absent in CI).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.channels.discord import DiscordChannel, _split_discord  # noqa: E402
from gateway.channels.discord_routing import (  # noqa: E402
    parse_action_custom_id,
    should_process_message,
)
from gateway.config import ChannelConfig, clear_config_cache  # noqa: E402


def _silent_log(_msg: str) -> None:
    pass


def _write_discord_config(instance: Path, *, chat_ids=(), blocked=()) -> None:
    ops = instance / "ops"
    ops.mkdir(parents=True, exist_ok=True)
    allow = "[" + ", ".join(f"'{c}'" for c in chat_ids) + "]"
    block = "[" + ", ".join(f"'{c}'" for c in blocked) + "]"
    (ops / "gateway.yaml").write_text(
        "channels:\n"
        "  discord:\n"
        "    enabled: true\n"
        "    bot_token_env: DISCORD_BOT_TOKEN\n"
        f"    chat_ids: {allow}\n"
        f"    blocked_chat_ids: {block}\n",
        encoding="utf-8",
    )
    clear_config_cache()


def _channel(instance: Path) -> DiscordChannel:
    cfg = ChannelConfig(enabled=True, bot_token_env="DISCORD_BOT_TOKEN")
    ch = DiscordChannel(instance, cfg, _silent_log)
    ch.bot_token = "test-token"
    return ch


class RoutingGateTests(unittest.TestCase):
    """`should_process_message` — the 'tag the bot to answer' guild rule."""

    def test_dm_always_answered(self):
        self.assertTrue(
            should_process_message(
                is_dm=True, mentioned=False, replied_to_bot=False, channel_allowlisted=False
            )
        )

    def test_guild_silent_without_mention(self):
        self.assertFalse(
            should_process_message(
                is_dm=False, mentioned=False, replied_to_bot=False, channel_allowlisted=False
            )
        )

    def test_guild_answers_on_mention(self):
        self.assertTrue(
            should_process_message(
                is_dm=False, mentioned=True, replied_to_bot=False, channel_allowlisted=False
            )
        )

    def test_guild_answers_on_reply_to_bot(self):
        self.assertTrue(
            should_process_message(
                is_dm=False, mentioned=False, replied_to_bot=True, channel_allowlisted=False
            )
        )

    def test_guild_allowlisted_channel_answers_always(self):
        self.assertTrue(
            should_process_message(
                is_dm=False, mentioned=False, replied_to_bot=False, channel_allowlisted=True
            )
        )


class CustomIdTests(unittest.TestCase):
    """Button ``custom_id`` contract: ``act:<verb>:<short_token>``."""

    def test_parse_stop(self):
        self.assertEqual(parse_action_custom_id("act:stop:abc123def456"), ("stop", "abc123def456"))

    def test_parse_background(self):
        self.assertEqual(parse_action_custom_id("act:bg:abc123def456"), ("bg", "abc123def456"))

    def test_ignores_unrelated(self):
        self.assertIsNone(parse_action_custom_id("dcauth:allow:999"))
        self.assertIsNone(parse_action_custom_id(""))
        self.assertIsNone(parse_action_custom_id("act:wat:tok"))
        self.assertIsNone(parse_action_custom_id("act:stop:"))

    def test_dead_placeholder_resolves_to_done_token(self):
        # The buttonless ``act:bg:done`` placeholder parses; the registry then
        # rejects the unknown 'done' token cleanly.
        self.assertEqual(parse_action_custom_id("act:bg:done"), ("bg", "done"))


class AuthTests(unittest.TestCase):
    """Default-deny authorization keyed by channel id or guild id."""

    def test_dm_implicitly_authorized(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_discord_config(instance, chat_ids=())
            ch = _channel(instance)
            self.assertTrue(ch._is_authorized("555", None, is_dm=True))

    def test_unknown_guild_channel_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_discord_config(instance, chat_ids=("111",))
            ch = _channel(instance)
            self.assertFalse(ch._is_authorized("999", "777", is_dm=False))

    def test_allowlisted_channel_authorized(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_discord_config(instance, chat_ids=("999",))
            ch = _channel(instance)
            self.assertTrue(ch._is_authorized("999", "777", is_dm=False))
            self.assertTrue(ch._is_channel_allowlisted("999"))

    def test_allowlisted_guild_authorizes_any_channel_but_not_always_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_discord_config(instance, chat_ids=("777",))  # guild id
            ch = _channel(instance)
            # any channel in guild 777 is authorized...
            self.assertTrue(ch._is_authorized("123", "777", is_dm=False))
            # ...but a guild-level allow is NOT a per-channel always-answer.
            self.assertFalse(ch._is_channel_allowlisted("123"))

    def test_blocklist_overrides_allow(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_discord_config(instance, chat_ids=("999",), blocked=("999",))
            ch = _channel(instance)
            self.assertFalse(ch._is_authorized("999", "777", is_dm=False))

    def test_blocked_guild_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_discord_config(instance, chat_ids=(), blocked=("777",))
            ch = _channel(instance)
            self.assertFalse(ch._is_authorized("123", "777", is_dm=False))


class ApprovePersistTests(unittest.TestCase):
    """The approve/block writers mutate yaml + bust the config cache."""

    def test_approve_then_block_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_discord_config(instance, chat_ids=())
            ch = _channel(instance)
            self.assertFalse(ch._is_authorized("999", "777", is_dm=False))
            ch._approve_channel("999")
            self.assertTrue(ch._is_authorized("999", "777", is_dm=False))
            ch._block_channel("999")
            self.assertFalse(ch._is_authorized("999", "777", is_dm=False))


class SplitTests(unittest.TestCase):
    """Outbound chunking replaces the old 2000-char truncate."""

    def test_short_text_single_chunk(self):
        self.assertEqual(_split_discord("hello"), ["hello"])

    def test_long_text_split_no_loss(self):
        text = ("para\n\n" * 600).strip()  # ~3600 chars
        chunks = _split_discord(text)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 2000)
        # No content silently dropped (Telegram parity, spec A1).
        self.assertEqual(sum(len(c) for c in chunks) >= len(text) - 4 * len(chunks), True)

    def test_oversize_single_block_hard_wrapped(self):
        text = "x" * 5000
        chunks = _split_discord(text)
        self.assertEqual("".join(chunks), text)
        for c in chunks:
            self.assertLessEqual(len(c), 2000)


class _FakeResponse:
    """Stand-in for ``interaction.response`` — records the ack text."""

    def __init__(self):
        self.acked: list[str] = []

    async def send_message(self, text, ephemeral=False):  # noqa: ANN001
        self.acked.append(text)


def _fake_interaction(*, custom_id, channel_id, guild_id, embed_desc="", content=""):
    embeds = [SimpleNamespace(description=embed_desc)] if embed_desc else []
    message = SimpleNamespace(id=4242, embeds=embeds, content=content)
    return SimpleNamespace(
        data={"custom_id": custom_id},
        channel_id=channel_id,
        guild_id=guild_id,
        message=message,
        response=_FakeResponse(),
    )


def _register_entry(token="abc123def456", session_id="abc123def456cafef00d"):
    from gateway import actions_registry

    actions_registry.register(
        short_token=token,
        session_id=session_id,
        child_pid=99999,
        slot_id=0,
        chat_id="123",
    )
    return session_id


class InteractionAuthTests(unittest.TestCase):
    """Button-click handler must authorize the channel before acting.

    The adversarial case: a click landing from a guild channel that is NOT on
    the allowlist must be rejected — no token resolution, no ``actions`` call.
    """

    def test_unauthorized_guild_click_does_not_call_actions(self):
        from gateway import actions

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_discord_config(instance, chat_ids=("111",))  # 999 NOT allowed
            ch = _channel(instance)
            _register_entry()
            interaction = _fake_interaction(
                custom_id="act:stop:abc123def456",
                channel_id="999",
                guild_id="777",
            )
            with patch.object(actions, "stop_session") as stop:
                asyncio.run(ch._on_interaction(object(), interaction))
            stop.assert_not_called()
            self.assertIn("Not authorized", interaction.response.acked)

    def test_authorized_channel_click_stops_session(self):
        from gateway import actions

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_discord_config(instance, chat_ids=("999",))  # channel allowed
            ch = _channel(instance)
            _register_entry()
            interaction = _fake_interaction(
                custom_id="act:stop:abc123def456",
                channel_id="999",
                guild_id="777",
                embed_desc="🛠️ building the thing",
            )
            fake_result = SimpleNamespace(ok=True, already_stopped=False)
            with patch.object(actions, "stop_session", return_value=fake_result) as stop, \
                 patch("supervisor.delivery.edit_card_discord", return_value=True) as edit:
                asyncio.run(ch._on_interaction(object(), interaction))
            stop.assert_called_once()
            # The finalized card preserves the original embed body (not just
            # the terminal suffix) — the cross-process card_text fallback.
            edit.assert_called_once()
            finalized = edit.call_args.kwargs["card"]
            self.assertIn("building the thing", finalized.text)
            self.assertIn("Stopped", finalized.text)
            # Terminal card carries no action token → buttons are dropped.
            self.assertFalse(getattr(finalized, "short_token", ""))

    def test_unknown_token_acks_session_ended(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_discord_config(instance, chat_ids=("999",))
            ch = _channel(instance)
            interaction = _fake_interaction(
                custom_id="act:stop:deadbeefdead",  # not registered
                channel_id="999",
                guild_id="777",
            )
            asyncio.run(ch._on_interaction(object(), interaction))
            self.assertIn("Session already ended", interaction.response.acked)

    def test_non_action_custom_id_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_discord_config(instance, chat_ids=("999",))
            ch = _channel(instance)
            interaction = _fake_interaction(
                custom_id="dcauth:allow:999", channel_id="999", guild_id="777"
            )
            asyncio.run(ch._on_interaction(object(), interaction))
            self.assertEqual(interaction.response.acked, [])


class ExtractCardTextTests(unittest.TestCase):
    """The clicked-message body recovery used to repaint the terminal card."""

    def test_prefers_embed_description(self):
        msg = SimpleNamespace(
            embeds=[SimpleNamespace(description="from embed")], content="from content"
        )
        self.assertEqual(DiscordChannel._extract_card_text(msg), "from embed")

    def test_falls_back_to_content(self):
        msg = SimpleNamespace(embeds=[], content="from content")
        self.assertEqual(DiscordChannel._extract_card_text(msg), "from content")

    def test_none_message(self):
        self.assertEqual(DiscordChannel._extract_card_text(None), "")


if __name__ == "__main__":
    unittest.main()
