"""Heartbeat builtin for the commitments engine."""

from __future__ import annotations

from pathlib import Path


def run(instance_dir: Path, dry_run: bool = False) -> dict:
    from commitments.engine import tick  # type: ignore

    return tick(instance_dir, dry_run=dry_run).as_dict()
