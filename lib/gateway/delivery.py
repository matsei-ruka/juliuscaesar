"""Outbound response delivery for the gateway runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .channels import deliver
from .channels.base import Channel, LogFn
from .config import ChannelConfig


def deliver_response(
    *,
    instance_dir: Path,
    source: str,
    response: str,
    meta: dict[str, Any],
    config_channels: dict[str, ChannelConfig],
    live_channels: dict[str, Channel],
    log: LogFn,
) -> str | None:
    """Send a response through a live channel when possible.

    Discord must use the live `discord.py` client and event loop. Other
    transports can fall back to the stateless registry sender when no live
    channel instance is available.
    """
    channel_name = str(meta.get("delivery_channel") or source)
    live_channel = live_channels.get(channel_name)
    if live_channel is not None:
        try:
            message_id = live_channel.send(response, meta)
        except Exception as exc:  # noqa: BLE001
            log(
                f"delivery failed channel={channel_name} reason={exc}",
                channel=channel_name,
                kind="delivery_failure",
            )
            message_id = None
        if message_id:
            return message_id
        log(
            f"delivery failed channel={channel_name} reason=send_returned_none",
            channel=channel_name,
            kind="delivery_failure",
        )
    if channel_name == "discord":
        if live_channel is None:
            log(
                "delivery failed channel=discord reason=no_live_channel",
                channel="discord",
                kind="delivery_failure",
            )
        return None
    message_id = deliver(
        instance_dir=instance_dir,
        source=source,
        response=response,
        meta=meta,
        config_channels=config_channels,
        log=log,
    )
    if message_id is None:
        log(
            f"delivery failed channel={channel_name} reason=stateless_send_returned_none",
            channel=channel_name,
            kind="delivery_failure",
        )
    return message_id
