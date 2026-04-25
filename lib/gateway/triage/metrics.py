"""Triage metrics — counters per class, average confidence, override rate."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .base import TriageResult


class MetricsRecorder:
    def __init__(self, instance_dir: Path):
        self.path = instance_dir / "state" / "gateway" / "triage-metrics.db"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                ts TEXT NOT NULL,
                class_ TEXT NOT NULL,
                brain TEXT NOT NULL,
                confidence REAL NOT NULL,
                outcome TEXT NOT NULL DEFAULT 'kept',
                fallback INTEGER NOT NULL DEFAULT 0,
                override INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_observations_ts ON observations(ts)"
        )
        return conn

    def record(self, result: TriageResult, *, fallback: bool = False, override: bool = False) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO observations(ts, class_, brain, confidence, outcome, fallback, override) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    result.class_,
                    result.brain,
                    float(result.confidence),
                    "fallback" if fallback else "override" if override else "kept",
                    1 if fallback else 0,
                    1 if override else 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def summary(self, *, hours: int = 24) -> dict:
        conn = self._connect()
        try:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=hours)
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
            rows = conn.execute(
                "SELECT class_, COUNT(*) AS n, AVG(confidence) AS avg_conf, "
                "SUM(fallback) AS fb, SUM(override) AS ov "
                "FROM observations WHERE ts >= ? GROUP BY class_",
                (cutoff,),
            ).fetchall()
            return {
                "since": cutoff,
                "by_class": [
                    {
                        "class": str(class_),
                        "count": int(n),
                        "avg_confidence": float(avg_conf or 0.0),
                        "fallbacks": int(fb or 0),
                        "overrides": int(ov or 0),
                    }
                    for class_, n, avg_conf, fb, ov in rows
                ],
            }
        finally:
            conn.close()
