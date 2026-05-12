"""Heartbeat builtin for silence-aware re-engagement."""

from __future__ import annotations

from pathlib import Path


def run(instance_dir: Path, dry_run: bool = False) -> dict:
    from reengage.queuer import run as run_reengage  # type: ignore

    return run_reengage(instance_dir, dry_run=dry_run).as_dict()
