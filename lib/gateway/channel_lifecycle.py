"""Gateway channel lifecycle management."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from .channels import build_enabled_channels
from .channels.base import Channel, LogFn
from .config import GatewayConfig


class ChannelLifecycle:
    """Owns live channel instances and their runner threads."""

    def __init__(
        self,
        instance_dir: Path,
        *,
        config: GatewayConfig,
        log: LogFn,
        enqueue: Callable[..., None],
        stop_requested: Callable[[], bool],
    ):
        self.instance_dir = instance_dir
        self.config = config
        self.log = log
        self.enqueue = enqueue
        self.stop_requested = stop_requested
        self.channels: dict[str, Channel] = {}
        self.threads: list[threading.Thread] = []

    def reload_config(self, config: GatewayConfig) -> None:
        self.config = config

    def start(self) -> None:
        for channel in build_enabled_channels(self.instance_dir, self.config, self.log):
            self.channels[channel.name] = channel
            thread = threading.Thread(
                target=channel.run,
                args=(self.enqueue, self.stop_requested),
                name=f"gateway-{channel.name}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)

    def close(self) -> None:
        for thread in self.threads:
            thread.join(timeout=2)
        self.threads.clear()
        for channel in list(self.channels.values()):
            close = getattr(channel, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # noqa: BLE001
                    self.log(f"channel close failed channel={channel.name}: {exc}")
        self.channels.clear()
