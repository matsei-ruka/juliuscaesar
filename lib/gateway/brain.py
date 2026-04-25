"""Compatibility shim — historical import path.

The original implementation lived here; Sprint 3 replaced it with per-brain
modules under `lib/gateway/brains/`. This module preserves the public symbols
that older tests / scripts import.
"""

from __future__ import annotations

from .brains.base import BrainResult
from .brains.dispatch import invoke_brain as _invoke_brain


def call_brain(*args, **kwargs):
    """Legacy entry point. New code should use `gateway.brains.invoke_brain`."""
    return _invoke_brain(*args, **kwargs)


__all__ = ["BrainResult", "call_brain"]
