"""Top-level dream orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .apply import apply_artifacts
from .codify import codify
from .consolidate import consolidate
from .reflect import reflect
from .report import write_report
from .schema import DreamResult


def run_dream(
    instance_dir: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    dry_run: bool = False,
) -> DreamResult:
    end = until or datetime.now(timezone.utc)
    dream_id = _dream_id(end, replay=since is not None)
    reflection = reflect(instance_dir, since=since, until=end)
    findings = consolidate(instance_dir, reflection)
    proposed = codify(reflection, findings)
    applied = apply_artifacts(instance_dir, proposed, dry_run=dry_run)
    status = "completed"
    report_path = write_report(
        instance_dir,
        dream_id=dream_id,
        reflection=reflection,
        findings=findings,
        artifacts=applied,
        status=status,
        dry_run=dry_run,
    )
    return DreamResult(
        dream_id=dream_id,
        report_path=report_path,
        reflection=reflection,
        findings=findings,
        artifacts=applied,
        status=status,
    )


def _dream_id(value: datetime, *, replay: bool = False) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    stamp = value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"replay-{stamp}" if replay else stamp
