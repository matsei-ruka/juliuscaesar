"""Commitment tick engine."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import actions
from .schema import Commitment, CommitmentError, dump, format_datetime, load, now_utc


Dispatcher = Callable[[Path, Commitment], actions.DispatchResult]


@dataclass
class TickSummary:
    ok: bool = True
    scanned: int = 0
    fired: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "scanned": self.scanned,
            "fired": self.fired,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": self.errors,
            "dry_run": self.dry_run,
        }


def commitments_dir(instance_dir: Path) -> Path:
    return instance_dir / "state" / "commitments"


def ensure_dirs(instance_dir: Path) -> None:
    root = commitments_dir(instance_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "done").mkdir(exist_ok=True)
    (root / "failed").mkdir(exist_ok=True)


def pending_path(instance_dir: Path, slug: str) -> Path:
    return commitments_dir(instance_dir) / f"{slug}.yaml"


def iter_pending(instance_dir: Path) -> list[Path]:
    ensure_dirs(instance_dir)
    return sorted(p for p in commitments_dir(instance_dir).glob("*.yaml") if p.is_file())


def add_commitment(instance_dir: Path, commitment: Commitment, *, overwrite: bool = False) -> Path:
    ensure_dirs(instance_dir)
    path = pending_path(instance_dir, commitment.slug)
    if path.exists() and not overwrite:
        raise CommitmentError(f"commitment already exists: {path}")
    dump(commitment, path)
    return path


def cancel_by_tag(instance_dir: Path, tag: str, *, dry_run: bool = False) -> list[str]:
    canceled: list[str] = []
    for path in iter_pending(instance_dir):
        try:
            commitment = load(path)
        except Exception:
            continue
        if tag not in commitment.tags:
            continue
        canceled.append(commitment.slug)
        if not dry_run:
            path.unlink(missing_ok=True)
    return canceled


def pending_with_tag(instance_dir: Path, tag: str) -> list[Commitment]:
    found: list[Commitment] = []
    for path in iter_pending(instance_dir):
        try:
            commitment = load(path)
        except Exception:
            continue
        if tag in commitment.tags:
            found.append(commitment)
    return found


def archived_with_tag(instance_dir: Path, tag: str) -> list[Commitment]:
    found: list[Commitment] = []
    root = commitments_dir(instance_dir)
    for subdir in (root / "done", root / "failed"):
        for path in sorted(subdir.glob("*.yaml")):
            try:
                commitment = load(path)
            except Exception:
                continue
            if tag in commitment.tags:
                found.append(commitment)
    return found


def tick(
    instance_dir: Path,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    dispatcher: Dispatcher = actions.dispatch,
) -> TickSummary:
    ensure_dirs(instance_dir)
    current = _as_utc(now or now_utc())
    summary = TickSummary(dry_run=dry_run)
    for path in iter_pending(instance_dir):
        summary.scanned += 1
        try:
            commitment = load(path)
        except Exception as exc:  # noqa: BLE001
            summary.ok = False
            summary.errors.append(f"{path.name}: {exc}")
            continue
        if _as_utc(commitment.due_at) > current:
            summary.skipped.append(commitment.slug)
            continue
        if dry_run:
            summary.fired.append(commitment.slug)
            continue
        result = dispatcher(instance_dir, commitment)
        if result.ok:
            _archive_success(instance_dir, path, commitment, current)
            summary.fired.append(commitment.slug)
            continue
        _handle_failure(instance_dir, path, commitment, result, current)
        summary.ok = False
        summary.failed.append(commitment.slug)
        summary.errors.append(f"{commitment.slug}: {result.message}")
    return summary


def _archive_success(
    instance_dir: Path,
    path: Path,
    commitment: Commitment,
    current: datetime,
) -> None:
    done = _archive_path(instance_dir, "done", commitment.slug, "executed", current)
    if commitment.repeat is None:
        path.replace(done)
        return
    shutil.copy2(path, done)
    next_due = commitment.due_at + _repeat_delta(commitment.repeat)
    updated = commitment.with_due_at(next_due).with_metadata(retries=0)
    dump(updated, path)


def _handle_failure(
    instance_dir: Path,
    path: Path,
    commitment: Commitment,
    result: actions.DispatchResult,
    current: datetime,
) -> None:
    retries = commitment.retries + 1
    updated = commitment.with_metadata(
        retries=retries,
        last_error=result.message,
        last_error_at=format_datetime(current),
    )
    if retries >= 3 or not result.retryable:
        dump(updated, path)
        failed = _archive_path(instance_dir, "failed", commitment.slug, "failed", current)
        path.replace(failed)
        return
    dump(updated, path)


def _archive_path(
    instance_dir: Path,
    bucket: str,
    slug: str,
    label: str,
    current: datetime,
) -> Path:
    root = commitments_dir(instance_dir) / bucket
    root.mkdir(parents=True, exist_ok=True)
    stamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = root / f"{slug}.{label}-{stamp}.yaml"
    counter = 2
    while candidate.exists():
        candidate = root / f"{slug}.{label}-{stamp}-{counter}.yaml"
        counter += 1
    return candidate


def _repeat_delta(repeat: str) -> timedelta:
    if repeat == "daily":
        return timedelta(days=1)
    if repeat == "weekly":
        return timedelta(days=7)
    raise CommitmentError(f"unsupported repeat: {repeat}")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
