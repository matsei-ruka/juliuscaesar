"""Heartbeat builtin for the dream pipeline."""

from __future__ import annotations

from pathlib import Path


def run(instance_dir: Path, dry_run: bool = False) -> dict:
    from dream.runner import run_dream  # type: ignore

    result = run_dream(instance_dir, dry_run=dry_run)
    return {
        "ok": result.status in {"completed", "partial"},
        "dream_id": result.dream_id,
        "artifacts": len(result.artifacts),
        "report": str(result.report_path) if result.report_path else None,
        "dry_run": dry_run,
    }
