"""Watchdog v2 — generic process supervisor.

Replaces the single-purpose `watchdog.sh` with a Python supervisor driven by
`ops/watchdog.yaml`. The legacy bash supervisor remains in `watchdog.sh` and
is the default until an instance opts in via `jc watchdog migrate`.

Public surface:
    Supervisor    — main loop, used by cli.py and tests.
    ChildSpec     — config record loaded from YAML.
    ChildState    — per-child runtime state, persisted to JSON.
    load_registry — read ops/watchdog.yaml into a list of ChildSpec.
"""

from .child import ChildSpec, ChildState
from .registry import load_registry, registry_path
from .supervisor import Supervisor

__all__ = [
    "ChildSpec",
    "ChildState",
    "Supervisor",
    "load_registry",
    "registry_path",
]
