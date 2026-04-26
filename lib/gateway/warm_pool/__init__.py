"""Warm pool of persistent claude REPL processes.

See `docs/specs/claude-warm-pool.md`. Phase 1 (MVP) only: PoolManager + PoolProcess
wrapping `claude -p --input-format=stream-json --output-format=stream-json`.
Opt-in via gateway config (`enable_warm_pool: true`).
"""

from .pool import PoolKey, PoolManager
from .process import PoolProcess, PoolProcessError
from .protocol import (
    InvokeResult,
    encode_user_message,
    parse_event_line,
)


__all__ = [
    "InvokeResult",
    "PoolKey",
    "PoolManager",
    "PoolProcess",
    "PoolProcessError",
    "encode_user_message",
    "parse_event_line",
]
