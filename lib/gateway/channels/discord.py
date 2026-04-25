"""Discord channel using the `discord.py` library.

Optional dependency: gated import. If `discord.py` is missing we log and
exit cleanly so the rest of the gateway keeps running. Inbound: DMs and
mentions; outbound: thread reply when applicable, otherwise channel reply.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, Callable

from ..config import ChannelConfig, env_value
from .base import EnqueueFn, LogFn


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

    def ready(self) -> bool:
        if not self.bot_token:
            return False
        try:
            import discord  # type: ignore  # noqa: F401
        except Exception:
            return False
        return True

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
            mentioned = client.user is not None and client.user in getattr(message, "mentions", [])
            is_dm = isinstance(getattr(message, "channel", None), discord.DMChannel)
            if not (mentioned or is_dm):
                return
            text = (message.content or "").strip()
            if not text:
                return
            channel_id = str(getattr(message.channel, "id", ""))
            thread_id = str(getattr(message.channel, "id", "")) if hasattr(message, "thread") else channel_id
            enqueue(
                source="discord",
                source_message_id=str(message.id),
                user_id=str(message.author.id),
                conversation_id=f"{channel_id}:{thread_id}",
                content=text,
                meta={
                    "channel_id": channel_id,
                    "guild_id": str(getattr(message.guild, "id", "")) if message.guild else None,
                    "is_dm": is_dm,
                    "reply_to_message_id": str(message.id),
                },
            )

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

        async def _send() -> str | None:
            channel = client.get_channel(int(channel_id_raw))
            if channel is None:
                channel = await client.fetch_channel(int(channel_id_raw))
            sent = await channel.send(response[:2000])
            return str(sent.id)

        future = asyncio.run_coroutine_threadsafe(_send(), loop)
        try:
            return future.result(timeout=15)
        except Exception as exc:  # noqa: BLE001
            self.log(f"discord send error: {exc}")
            return None
