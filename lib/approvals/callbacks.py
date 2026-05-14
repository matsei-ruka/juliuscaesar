"""Apply-callback registry — producers register `(kind) -> handler` here."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .models import Approval


ApplyCallback = Callable[[Path, Approval], dict | None]


_REGISTRY: dict[str, ApplyCallback] = {}


def register(callback_kind: str, handler: ApplyCallback) -> None:
    """Register or replace the apply-callback for `callback_kind`."""
    _REGISTRY[callback_kind] = handler


def unregister(callback_kind: str) -> None:
    _REGISTRY.pop(callback_kind, None)


def get(callback_kind: str) -> ApplyCallback | None:
    return _REGISTRY.get(callback_kind)


def registered_kinds() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY.keys()))


def clear_registry() -> None:
    """Test helper: drop every registered callback."""
    _REGISTRY.clear()


def ensure_defaults_loaded() -> None:
    """Import producer modules so they register their callbacks.

    Idempotent. Each producer module guards itself against double-import.
    """
    try:
        from . import dispatch  # noqa: F401 — registers internal handlers
    except Exception:
        pass
