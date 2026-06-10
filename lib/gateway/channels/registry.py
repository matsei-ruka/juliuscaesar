"""Channel registry: discovery, build, deliver."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any, Callable

from ..config import ChannelConfig, GatewayConfig
from .base import Channel, LogFn
from .company_inbox import CompanyInboxChannel
from .cron import CronChannel
from .discord import DiscordChannel
from .email import EmailChannel
from .jc_events import JcEventsChannel
from .slack import SlackSocketModeChannel
from .telegram import TelegramChannel
from .voice import VoiceChannel
from .whatsapp import WhatsAppChannel


_CHANNEL_FACTORIES = {
    "telegram": TelegramChannel,
    "slack": SlackSocketModeChannel,
    "discord": DiscordChannel,
    "voice": VoiceChannel,
    "jc-events": JcEventsChannel,
    "cron": CronChannel,
    "email": EmailChannel,
    "company-inbox": CompanyInboxChannel,
    "whatsapp": WhatsAppChannel,
}


def enabled_channel_factories(
    instance_dir: Path,
    config: GatewayConfig,
    log: LogFn,
) -> dict[str, "Callable[[], Channel]"]:
    """Zero-arg rebuild closure per enabled channel (audit feature 4).

    The supervisor in ``channel_lifecycle`` rebuilds a crashed channel from
    its factory before restarting it, so a wedged instance never gets reused.
    """
    factories: dict[str, Callable[[], Channel]] = {}
    for name, cls in _CHANNEL_FACTORIES.items():
        cfg = config.channel(name)
        if not cfg.enabled:
            continue
        factories[name] = partial(cls, instance_dir, cfg, log)
    return factories


def build_enabled_channels(
    instance_dir: Path,
    config: GatewayConfig,
    log: LogFn,
) -> list[Channel]:
    """Return live Channel instances for every enabled channel.

    Constructor failures are isolated per channel (audit feature 4 / B-P1):
    one bad channel no longer kills the gateway at boot.
    """

    channels: list[Channel] = []
    for name, factory in enabled_channel_factories(instance_dir, config, log).items():
        try:
            channels.append(factory())
        except Exception as exc:  # noqa: BLE001 — per-channel isolation
            log(f"channel build failed name={name}: {exc!r} — skipped")
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
    try:
        message_id = instance.send(response, meta)
    except Exception as exc:  # noqa: BLE001
        log(f"delivery failed source={source} channel={channel} reason={exc}")
        return None
    if message_id is None:
        log(f"delivery failed source={source} channel={channel} reason=send_returned_none")
    return message_id
