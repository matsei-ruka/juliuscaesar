"""Base contract for gateway channels."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


EnqueueFn = Callable[..., None]
LogFn = Callable[[str], None]


@dataclass(frozen=True)
class DeliveryTarget:
    channel: str
    conversation_id: str | None
    meta: dict[str, Any]


class Channel(Protocol):
    name: str

    def ready(self) -> bool: ...

    def run(self, enqueue: EnqueueFn, should_stop: Callable[[], bool]) -> None: ...

    def send(self, response: str, meta: dict[str, Any]) -> str | None: ...
