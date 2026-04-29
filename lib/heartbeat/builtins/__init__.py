"""Built-in heartbeat tasks — pure-Python tasks that don't dispatch to an LLM.

Each builtin is a callable ``run(instance_dir, dry_run=False) -> dict`` that
mutates instance state and returns a JSON-serializable summary. The runner
detects ``builtin: <name>`` in tasks.yaml and dispatches here instead of
spawning an adapter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import hot_tidy as _hot_tidy


BuiltinFn = Callable[[Path, bool], dict]


_BUILTINS: dict[str, BuiltinFn] = {
    "hot_tidy": _hot_tidy.run,
}


def get(name: str) -> BuiltinFn | None:
    return _BUILTINS.get(name)


def names() -> tuple[str, ...]:
    return tuple(_BUILTINS.keys())


__all__ = ["get", "names", "BuiltinFn"]
