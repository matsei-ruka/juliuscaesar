"""Outbound response delivery for the gateway runtime."""

from __future__ import annotations

import socket
import urllib.error
from pathlib import Path
from typing import Any

from .channels import deliver
from .channels.base import Channel, LogFn
from .config import ChannelConfig


class DeliveryAmbiguous(Exception):
    """Live-channel send raised AFTER the request may have been accepted.

    Raised only under ``strict_idempotency``: the caller must keep its
    delivery-ledger reservation (blocking any resend of this event/channel)
    and must NOT fall back to a second stateless send — a read timeout after
    Telegram accepted the request is a confirmed duplicate-reply source
    (audit Finding F: delivery.py fallback).
    """


def _provably_undelivered(exc: BaseException) -> bool:
    """True iff ``exc`` proves the request never reached the channel API.

    Pre-connect failures (refused connection, DNS resolution) cannot have
    delivered anything — a retry/fallback send is safe. Timeouts and unknown
    exceptions may have fired after the API accepted the send → ambiguous.
    """
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return False
    if isinstance(exc, (ConnectionRefusedError, socket.gaierror)):
        return True
    if isinstance(exc, urllib.error.URLError) and not isinstance(
        exc, urllib.error.HTTPError
    ):
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return False
        if isinstance(reason, (ConnectionRefusedError, ConnectionError, socket.gaierror)):
            return True
    return False


def deliver_response(
    *,
    instance_dir: Path,
    source: str,
    response: str,
    meta: dict[str, Any],
    config_channels: dict[str, ChannelConfig],
    live_channels: dict[str, Channel],
    log: LogFn,
    strict_idempotency: bool = False,
) -> str | None:
    """Send a response through a live channel when possible.

    Discord must use the live `discord.py` client and event loop. Other
    transports can fall back to the stateless registry sender when no live
    channel instance is available.

    ``strict_idempotency`` (set only by the ledger-gated reply path): a live
    send that raises ambiguously (timeout / unknown — the request may have
    been accepted) raises :class:`DeliveryAmbiguous` instead of falling back
    to a second stateless send. Provably-pre-delivery failures still fall
    back. Default keeps legacy always-fallback behavior for notices and
    other unledgered call sites.
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
            if strict_idempotency and not _provably_undelivered(exc):
                raise DeliveryAmbiguous(
                    f"live send raised post-connect on channel={channel_name}: {exc}"
                ) from exc
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
