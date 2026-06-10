"""Machine-readable instance snapshot for `jc doctor --json` (audit feature 10).

PID-up ≠ serving: the manual fleet sweeps (fleet-health skill) shell into 24
hosts and grep logs because nothing exposes queue depth, oldest-queued age,
or brain failure state as a checkable signal. This module reads everything
from disk (read-only sqlite + json files) — it never touches the running
gateway.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def queue_metrics(instance_dir: Path) -> dict[str, Any]:
    """Depth by status + oldest-queued age, from queue.db (read-only)."""
    db = instance_dir / "state" / "gateway" / "queue.db"
    metrics: dict[str, Any] = {
        "db_present": db.exists(),
        "depth_by_status": {},
        "oldest_queued_age_seconds": None,
        "failed_count": 0,
    }
    if not db.exists():
        return metrics
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error as exc:
        metrics["error"] = str(exc)
        return metrics
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM events GROUP BY status"
        ).fetchall()
        metrics["depth_by_status"] = {row["status"]: row["n"] for row in rows}
        metrics["failed_count"] = int(metrics["depth_by_status"].get("failed", 0))
        oldest = conn.execute(
            "SELECT MIN(available_at) AS ts FROM events WHERE status = 'queued'"
        ).fetchone()
        ts = _parse_iso(oldest["ts"] if oldest else None)
        if ts is not None:
            metrics["oldest_queued_age_seconds"] = max(0.0, time.time() - ts)
    except sqlite3.Error as exc:
        metrics["error"] = str(exc)
    finally:
        conn.close()
    return metrics


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _file_age_seconds(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def snapshot(instance_dir: Path) -> dict[str, Any]:
    """Everything `jc doctor --json` reports. Pure disk reads."""
    from .liveness import gateway_pid_finding

    state = instance_dir / "state" / "gateway"
    pid = gateway_pid_finding(instance_dir)
    return {
        "instance_dir": str(instance_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gateway": {"pid_check": pid.level, "pid_detail": pid.message},
        "heartbeat_age_seconds": _file_age_seconds(state / "heartbeat"),
        "liveness": _read_json(state / "liveness.json"),
        "queue": queue_metrics(instance_dir),
        "brains_failed": _read_json(state / "brain_failure.json") or {},
        "channel_health": _read_json(state / "channel_health.json") or {},
    }
