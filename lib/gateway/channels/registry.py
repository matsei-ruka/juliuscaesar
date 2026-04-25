"""Channel registry: discovery, build, deliver."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import ChannelConfig, GatewayConfig
from .base import Channel, LogFn
from .cron import CronChannel
from .discord import DiscordChannel
from .jc_events import JcEventsChannel
from .slack import SlackSocketModeChannel
from .telegram import TelegramChannel
from .voice import VoiceChannel


_CHANNEL_FACTORIES = {
    "telegram": TelegramChannel,
    "slack": SlackSocketModeChannel,
    "discord": DiscordChannel,
    "voice": VoiceChannel,
    "jc-events": JcEventsChannel,
    "cron": CronChannel,
}


def build_enabled_channels(
    instance_dir: Path,
    config: GatewayConfig,
    log: LogFn,
) -> list[Channel]:
    """Return live Channel instances for every enabled channel."""

    channels: list[Channel] = []
    for name, factory in _CHANNEL_FACTORIES.items():
        cfg = config.channel(name)
        if not cfg.enabled:
            continue
        channels.append(factory(instance_dir, cfg, log))
    return channels


def deliver(
    *,
    instance_dir: Path,
    source: str,
    response: str,
    meta: dict[str, Any],
    config_channels: dict[str, ChannelConfig],
    log: LogFn,
) -> str | None:
    channel = meta.get("delivery_channel") or source
    factory = _CHANNEL_FACTORIES.get(str(channel))
    if factory is None:
        log(f"delivery skipped for source={source}")
        return None
    cfg = config_channels.get(str(channel)) or ChannelConfig()
    instance = factory(instance_dir, cfg, log)
    return instance.send(response, meta)
