"""Discord channel using the `discord.py` library.

Optional dependency: gated import. If `discord.py` is missing we log and
exit cleanly so the rest of the gateway keeps running.

Parity with Telegram (docs/specs/discord-parity.md):

  * **Auth** — default-deny allowlist keyed by channel id (or guild id) via
    ``channels.discord.chat_ids`` / ``blocked_chat_ids``. A first message from
    an unknown channel triggers an operator approval prompt (sent to the
    Telegram main DM — the single approval surface). Mirrors
    ``telegram.py``'s ``_is_authorized`` / sender-approval flow.
  * **Mention-gating** — in guild channels the bot answers only when
    @mentioned, replied-to, or the channel is explicitly allowlisted. DMs
    always answer. (``discord_routing.should_process_message``.)
  * **Supervisor cards** — sent as embeds with Stop/Background button
    components by ``supervisor.delivery``; this channel hosts the interaction
    handler (the twin of Telegram's callback-query path) that turns a button
    click into an ``actions`` call.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Callable

from ..config import ChannelConfig, env_value, load_config_cached
from ..config_writer import update_gateway_yaml_chat_lists
from ._http import http_json
from .base import EnqueueFn, LogFn
from .discord_routing import parse_action_custom_id, should_process_message

_DISCORD_API = "https://discord.com/api/v10"
_DISCORD_TEXT_LIMIT = 2000


class DiscordChannel:
    name = "discord"

    def __init__(self, instance_dir: Path, cfg: ChannelConfig, log: LogFn):
        self.instance_dir = instance_dir
        self.cfg = cfg
        self.log = log
        self.bot_token = env_value(instance_dir, cfg.bot_token_env or "DISCORD_BOT_TOKEN")
        self._client = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_lock = threading.Lock()
        # Suppress duplicate approval prompts within this process lifetime.
        self._auth_prompts_sent: set[str] = set()

    def ready(self) -> bool:
        if not self.bot_token:
            return False
        try:
            import discord  # type: ignore  # noqa: F401
        except Exception:
            return False
        return True

    # ------------------------------------------------------------------
    # Authorization (config-only, default-deny) — twin of telegram.py
    # ------------------------------------------------------------------

    def _authority_sets(self) -> tuple[frozenset[str], frozenset[str]]:
        """Return ``(allowed, blocked)`` id sets from ``channels.discord``.

        Ids may be Discord **channel ids** or **guild ids** — a guild id on
        the allowlist authorizes every channel in that server. Config is the
        only source of truth (no DB read on this path), written by the
        operator approval flow.
        """
        try:
            cfg = load_config_cached(self.instance_dir).channel("discord")
        except Exception as exc:  # noqa: BLE001
            self.log(f"discord authority load failed: {exc}")
            return frozenset(), frozenset()
        allowed = {str(x) for x in cfg.chat_ids if str(x)}
        blocked = {str(x) for x in cfg.blocked_chat_ids if str(x)}
        return frozenset(allowed), frozenset(blocked)

    def _is_authorized(self, channel_id: str, guild_id: str | None, is_dm: bool) -> bool:
        """Default-deny. DMs are implicitly allowed; guild traffic needs the
        channel id OR its guild id on the allowlist and not on the blocklist.
        """
        if is_dm:
            return True
        allowed, blocked = self._authority_sets()
        if channel_id in blocked or (guild_id and guild_id in blocked):
            return False
        if channel_id in allowed:
            return True
        if guild_id and guild_id in allowed:
            return True
        return False

    def _is_channel_allowlisted(self, channel_id: str) -> bool:
        """True iff the *specific* channel id is allowlisted → always answer
        (no @mention required), the Discord analog of a 1:1 Telegram chat.
        Guild-level allows still require a mention.
        """
        allowed, _blocked = self._authority_sets()
        return channel_id in allowed

    def _approve_channel(self, channel_id: str) -> None:
        update_gateway_yaml_chat_lists(
            self.instance_dir,
            channel="discord",
            allow_add=[channel_id],
            block_remove=[channel_id],
        )
        from ..config import clear_config_cache

        clear_config_cache()

    def _block_channel(self, channel_id: str) -> None:
        update_gateway_yaml_chat_lists(
            self.instance_dir,
            channel="discord",
            block_add=[channel_id],
            allow_remove=[channel_id],
        )
        from ..config import clear_config_cache

        clear_config_cache()

    # ------------------------------------------------------------------
    # Operator approval prompt (delivered to the Telegram main DM)
    # ------------------------------------------------------------------

    def _operator_telegram_dm(self) -> str | None:
        """Resolve the operator's Telegram main DM for approval prompts.

        Single approval surface (spec §5.2): Discord access requests land in
        the operator's existing Telegram inbox. Resolution order mirrors
        ``telegram.py``: ``TELEGRAM_CHAT_ID`` env → ``principal.telegram_chat_id``
        → first ``channels.telegram.chat_ids``.
        """
        env = env_value(self.instance_dir, "TELEGRAM_CHAT_ID")
        if env:
            return str(env)
        try:
            cfg = load_config_cached(self.instance_dir)
        except Exception:  # noqa: BLE001
            return None
        if cfg.principal.telegram_chat_id:
            return str(cfg.principal.telegram_chat_id)
        for chat_id in cfg.channel("telegram").chat_ids:
            if str(chat_id):
                return str(chat_id)
        return None

    def _maybe_send_approval_prompt(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        guild_name: str,
        channel_name: str,
        member_count: int | None,
        preview: str,
    ) -> None:
        """Prompt the operator (via Telegram) to allow/deny a new channel.

        Idempotent within the process lifetime. The Allow/Deny taps are
        Telegram inline buttons whose ``callback_data`` carries the ``dcauth:``
        prefix; ``telegram.py`` routes the tap back into
        ``update_gateway_yaml_chat_lists(channel="discord", ...)``.
        """
        if channel_id in self._auth_prompts_sent:
            return
        allowed, blocked = self._authority_sets()
        if channel_id in allowed or channel_id in blocked:
            return
        tg_token = env_value(self.instance_dir, "TELEGRAM_BOT_TOKEN")
        operator = self._operator_telegram_dm()
        if not tg_token or not operator:
            self.log("discord approval prompt skipped: no telegram operator DM")
            return
        member_blurb = f"{member_count} members" if member_count is not None else "?"
        preview_blurb = ""
        if preview:
            snippet = preview[:100].replace("\n", " ").strip()
            if len(preview) > 100:
                snippet += "…"
            preview_blurb = f'Preview: "{snippet}"\n\n'
        body = (
            "New Discord channel — approve?\n\n"
            f"{guild_name} › #{channel_name}\n"
            f"channel_id: {channel_id}\n"
            f"guild_id: {guild_id or '-'} ({member_blurb})\n\n"
            f"{preview_blurb}"
            "Tap Allow to process messages from this channel."
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Allow", "callback_data": f"dcauth:allow:{channel_id}"},
                {"text": "⛔ Deny", "callback_data": f"dcauth:deny:{channel_id}"},
            ]]
        }
        payload = {
            "chat_id": operator,
            "text": body,
            "disable_web_page_preview": True,
            "reply_markup": json.dumps(keyboard),
        }
        try:
            data = http_json(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                data=payload,
                timeout=15,
            )
            ok = bool(data.get("ok"))
        except Exception as exc:  # noqa: BLE001
            self.log(f"discord approval prompt failed channel_id={channel_id}: {exc}")
            return
        if ok:
            self._auth_prompts_sent.add(channel_id)
            self.log(f"discord approval prompt sent channel_id={channel_id}")

    # ------------------------------------------------------------------
    # Inbound + interaction event loop
    # ------------------------------------------------------------------

    def run(self, enqueue: EnqueueFn, should_stop: Callable[[], bool]) -> None:
        if not self.bot_token:
            self.log("discord disabled: DISCORD_BOT_TOKEN missing")
            return
        try:
            import discord  # type: ignore
        except Exception:
            self.log("discord disabled: install discord.py to enable")
            return

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_message(message):  # type: ignore[no-untyped-def]
            if message.author.bot:
                return
            try:
                await self._on_message(client, discord, message, enqueue)
            except Exception as exc:  # noqa: BLE001
                # discord.py swallows handler exceptions to stderr; route them
                # to our channel log instead (parity with on_interaction) so a
                # malformed payload is observable and never escalates.
                self.log(f"discord message error: {exc}")

        @client.event
        async def on_interaction(interaction):  # type: ignore[no-untyped-def]
            try:
                await self._on_interaction(client, interaction)
            except Exception as exc:  # noqa: BLE001
                self.log(f"discord interaction error: {exc}")

        async def runner() -> None:
            with self._loop_lock:
                self._loop = asyncio.get_running_loop()
            await client.start(self.bot_token)

        async def watcher() -> None:
            while not should_stop():
                await asyncio.sleep(1)
            await client.close()

        async def main() -> None:
            try:
                await asyncio.gather(runner(), watcher())
            except Exception as exc:  # noqa: BLE001
                self.log(f"discord error: {exc}")

        try:
            asyncio.run(main())
        except RuntimeError as exc:
            self.log(f"discord runner error: {exc}")
        self.log("discord channel stopped")

    async def _on_message(self, client, discord, message, enqueue: EnqueueFn) -> None:
        """Authorize → gate → enqueue. Mirrors the Telegram poll-loop body."""
        channel = getattr(message, "channel", None)
        channel_id = str(getattr(channel, "id", ""))
        is_dm = isinstance(channel, discord.DMChannel) or message.guild is None
        guild = message.guild
        guild_id = str(getattr(guild, "id", "")) if guild else None
        text = (message.content or "").strip()

        # Hard short-circuit on the blocklist before any work.
        allowed, blocked = self._authority_sets()
        if not is_dm and (channel_id in blocked or (guild_id and guild_id in blocked)):
            self.log(f"discord dropped blocked channel_id={channel_id}")
            return

        if not self._is_authorized(channel_id, guild_id, is_dm):
            self.log(f"discord ignored unauthorized channel_id={channel_id}")
            guild_name = str(getattr(guild, "name", "")) if guild else "DM"
            channel_name = str(getattr(channel, "name", "")) or channel_id
            member_count = getattr(guild, "member_count", None) if guild else None
            self._maybe_send_approval_prompt(
                channel_id=channel_id,
                guild_id=guild_id,
                guild_name=guild_name,
                channel_name=channel_name,
                member_count=member_count,
                preview=text,
            )
            return

        mentioned = client.user is not None and client.user in getattr(message, "mentions", [])
        replied_to_bot = self._is_reply_to_bot(client, message)
        if not should_process_message(
            is_dm=is_dm,
            mentioned=mentioned,
            replied_to_bot=replied_to_bot,
            channel_allowlisted=self._is_channel_allowlisted(channel_id),
        ):
            self.log(f"discord ignored non-mention channel_id={channel_id}")
            return

        if not text:
            return
        thread_id = channel_id
        enqueue(
            source="discord",
            source_message_id=str(message.id),
            user_id=str(message.author.id),
            conversation_id=f"{channel_id}:{thread_id}",
            content=text,
            meta={
                "channel_id": channel_id,
                "guild_id": guild_id,
                "is_dm": is_dm,
                "reply_to_message_id": str(message.id),
            },
        )

    @staticmethod
    def _is_reply_to_bot(client, message) -> bool:
        """Best-effort: True iff ``message`` replies to one of the bot's
        messages (the Discord analog of Telegram's reply-to-bot rule)."""
        ref = getattr(message, "reference", None)
        if ref is None or client.user is None:
            return False
        resolved = getattr(ref, "resolved", None)
        author = getattr(resolved, "author", None)
        return author is not None and getattr(author, "id", None) == client.user.id

    @staticmethod
    def _extract_card_text(message) -> str:
        """Recover a card's body from the clicked Discord message.

        Action cards render as an embed (description holds the body); plain
        cards use ``content``. Prefer the embed description, fall back to
        content. Twin of telegram.py's ``_extract_message_text``.
        """
        if message is None:
            return ""
        for emb in getattr(message, "embeds", None) or []:
            desc = getattr(emb, "description", None)
            if desc:
                return str(desc)
        return str(getattr(message, "content", "") or "")

    async def _on_interaction(self, client, interaction) -> None:
        """Handle a Stop/Background button click — twin of the Telegram
        callback-query handler. Resolves the token to a running session and
        calls ``actions``; runs the blocking work off the event loop.
        """
        data = getattr(interaction, "data", None) or {}
        custom_id = data.get("custom_id") or ""
        parsed = parse_action_custom_id(custom_id)
        if parsed is None:
            return
        verb, short_token = parsed

        channel_id = str(getattr(interaction, "channel_id", "") or "")
        guild_id = getattr(interaction, "guild_id", None)
        guild_id = str(guild_id) if guild_id else None
        is_dm = guild_id is None
        if not self._is_authorized(channel_id, guild_id, is_dm):
            await self._ack_interaction(interaction, "Not authorized")
            self.log(f"discord interaction unauthorized channel_id={channel_id}")
            return

        from .. import actions_registry

        entry = actions_registry.resolve(short_token)
        if entry is None:
            await self._ack_interaction(interaction, "Session already ended")
            return
        if actions_registry.check_and_set_debounce(entry.session_id):
            await self._ack_interaction(interaction, "")
            return

        # Ack within Discord's 3s window, then do the real (blocking) work in
        # a thread so the event loop is never stalled by SIGTERM grace.
        await self._ack_interaction(
            interaction, "Stopping…" if verb == "stop" else "Backgrounding…"
        )
        loop = asyncio.get_running_loop()
        msg_obj = getattr(interaction, "message", None)
        message_id = str(getattr(msg_obj, "id", "") or "")
        # ``entry.card_text`` is set by the *supervisor* process and never
        # reaches the gateway's in-memory registry, so it is always empty
        # here. Recover the original body straight off the clicked message
        # (embed description, or plain content) — the Discord analog of
        # telegram.py's ``_extract_message_text(msg)`` fallback. Without this
        # the finalized card collapses to just the suffix.
        orig_text = self._extract_card_text(msg_obj)
        if verb == "stop":
            await loop.run_in_executor(
                None, self._do_stop, entry, channel_id, message_id, orig_text
            )
        else:
            await loop.run_in_executor(
                None, self._do_background, entry, channel_id, message_id, orig_text
            )

    async def _ack_interaction(self, interaction, text: str) -> None:
        """Send an ephemeral acknowledgement; best-effort across discord.py
        versions (defer + followup, or a direct ephemeral response)."""
        try:
            response = getattr(interaction, "response", None)
            if response is not None and hasattr(response, "send_message"):
                await response.send_message(text or "✓", ephemeral=True)
                return
            if response is not None and hasattr(response, "defer"):
                await response.defer(ephemeral=True)
        except Exception as exc:  # noqa: BLE001
            self.log(f"discord interaction ack failed: {exc}")

    def _do_stop(
        self, entry, channel_id: str, message_id: str, orig_text: str = ""
    ) -> None:
        from .. import actions

        grace = self._action_stop_grace_seconds()
        result = actions.stop_session(
            entry.session_id,
            stop_grace_seconds=grace,
            instance_dir=self.instance_dir,
            actor_chat_id=channel_id,
        )
        suffix = _stopped_suffix(entry)
        self._finalize_card(entry, channel_id, message_id, suffix, orig_text)
        self.log(
            f"discord action stop session={entry.session_id[:12]} "
            f"ok={result.ok} already_stopped={result.already_stopped}"
        )

    def _do_background(
        self, entry, channel_id: str, message_id: str, orig_text: str = ""
    ) -> None:
        from .. import actions

        result = actions.background_session(
            entry.session_id,
            chat_id=channel_id,
            supervisor_msg_id=None,
            max_per_chat=self._action_max_background_per_chat(),
            instance_dir=self.instance_dir,
            actor_chat_id=channel_id,
        )
        if result.capped or result.already_backgrounded or not result.ok:
            self.log(
                f"discord action background noop session={entry.session_id[:12]} "
                f"capped={result.capped} already={result.already_backgrounded} ok={result.ok}"
            )
            return
        suffix = _backgrounded_suffix()
        self._finalize_card(entry, channel_id, message_id, suffix, orig_text)
        self.log(f"discord action background session={entry.session_id[:12]}")

    def _finalize_card(
        self,
        entry,
        channel_id: str,
        message_id: str,
        suffix: str,
        orig_text: str = "",
    ) -> None:
        """Append the terminal suffix to the card and drop its buttons.

        Edits via the REST card writer with a buttonless Card (short_token
        cleared) so the action row is removed once a decision lands. The
        original body comes from the clicked message (``orig_text``) because
        ``entry.card_text`` is cross-process and empty in the gateway.
        """
        if not message_id:
            return
        from supervisor.cards import Card
        from supervisor.delivery import edit_card_discord

        original = (orig_text or getattr(entry, "card_text", "") or "").rstrip()
        new_text = f"{original}\n\n{suffix}" if original else suffix
        card = Card(text=new_text, phase="stopped", emoji="⏹", language="en")
        edit_card_discord(
            instance_dir=self.instance_dir,
            channel_id=channel_id,
            message_id=message_id,
            card=card,
            log=self.log,
        )

    def _action_stop_grace_seconds(self) -> int:
        try:
            return int(load_config_cached(self.instance_dir).actions.stop_grace_seconds)
        except Exception:  # noqa: BLE001
            return 5

    def _action_max_background_per_chat(self) -> int:
        try:
            return int(load_config_cached(self.instance_dir).actions.max_background_per_chat)
        except Exception:  # noqa: BLE001
            return 3

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    def send(self, response: str, meta: dict[str, Any]) -> str | None:
        if not response.strip():
            return None
        try:
            import discord  # type: ignore  # noqa: F401
        except Exception:
            return None
        client = self._client
        loop = self._loop
        if client is None or loop is None:
            return None

        channel_id_raw = meta.get("channel_id") or meta.get("notify_chat_id")
        if not channel_id_raw:
            return None

        chunks = _split_discord(response)

        async def _send() -> str | None:
            channel = client.get_channel(int(channel_id_raw))
            if channel is None:
                channel = await client.fetch_channel(int(channel_id_raw))
            last_id: str | None = None
            for chunk in chunks:
                sent = await channel.send(chunk)
                last_id = str(sent.id)
            return last_id

        future = asyncio.run_coroutine_threadsafe(_send(), loop)
        try:
            return future.result(timeout=30)
        except Exception as exc:  # noqa: BLE001
            self.log(f"discord send error: {exc}")
            return None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _split_discord(text: str, limit: int = _DISCORD_TEXT_LIMIT) -> list[str]:
    """Split ``text`` into <=limit chunks, paragraph-aware, never truncating.

    Replaces the old hard ``response[:2000]`` slice (Telegram parity, spec
    A1). Splits on blank lines first, then hard-wraps any oversize block.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(block) <= limit:
            current = block
            continue
        for i in range(0, len(block), limit):
            piece = block[i : i + limit]
            if len(piece) == limit:
                chunks.append(piece)
            else:
                current = piece
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]


def _stopped_suffix(entry) -> str:
    from datetime import datetime, timezone
    import time

    hhmmss = datetime.now(timezone.utc).strftime("%H:%M:%S")
    try:
        duration = max(0, int(time.time() - float(entry.started_at)))
    except (AttributeError, TypeError, ValueError):
        duration = 0
    return f"✋ Stopped at {hhmmss} UTC · {duration // 60:02d}:{duration % 60:02d}"


def _backgrounded_suffix() -> str:
    from datetime import datetime, timezone

    hhmmss = datetime.now(timezone.utc).strftime("%H:%M:%S")
    return f"🔄 Backgrounded at {hhmmss} UTC"
