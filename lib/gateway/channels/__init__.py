"""Gateway channel package.

Per-channel modules live alongside this file. The registry constructs the
enabled channels at runtime; `deliver` is the dispatch point used by the
runtime to send a response back through the originating channel.

Re-exports preserve the older flat-module import paths used by callers that
imported `lib.gateway.channels.TelegramChannel` directly.
"""

from .base import Channel, DeliveryTarget, EnqueueFn, LogFn
from .cron import CronChannel
from .discord import DiscordChannel
from .jc_events import JcEventsChannel
from .registry import build_enabled_channels, deliver
from .slack import SlackSocketModeChannel
from .telegram import TelegramChannel
from .voice import VoiceChannel


__all__ = [
    "Channel",
    "CronChannel",
    "DeliveryTarget",
    "DiscordChannel",
    "EnqueueFn",
    "JcEventsChannel",
    "LogFn",
    "SlackSocketModeChannel",
    "TelegramChannel",
    "VoiceChannel",
    "build_enabled_channels",
    "deliver",
]
