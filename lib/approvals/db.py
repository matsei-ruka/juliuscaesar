"""SQLite store for the unified approval table (`state/approvals.db`)."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import Approval


SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
  approval_id        TEXT PRIMARY KEY,
  kind               TEXT NOT NULL,
  title              TEXT NOT NULL,
  body               TEXT NOT NULL DEFAULT '',
  payload            TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'pending',
  requested_at       TEXT NOT NULL,
  decided_at         TEXT,
  decided_by         TEXT,
  decision_channel   TEXT,
  expires_at         TEXT,
  applied_at         TEXT,
  callback_token     TEXT NOT NULL,
  callback_kind      TEXT NOT NULL,
  callback_payload   TEXT NOT NULL DEFAULT '{}',
  producer           TEXT NOT NULL,
  source_ref         TEXT,
  notify_telegram    INTEGER NOT NULL DEFAULT 1,
  notify_email       INTEGER NOT NULL DEFAULT 0,
  notified_at        TEXT,
  result             TEXT,
  note               TEXT,
  media_paths        TEXT NOT NULL DEFAULT '[]',
  schema_version     INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_approvals_status  ON approvals (status, requested_at);
CREATE INDEX IF NOT EXISTS idx_approvals_kind    ON approvals (kind, status);
CREATE INDEX IF NOT EXISTS idx_approvals_source  ON approvals (source_ref);
CREATE INDEX IF NOT EXISTS idx_approvals_expires ON approvals (expires_at)
  WHERE status = 'pending';
"""


def db_path(instance_dir: Path) -> Path:
    return Path(instance_dir) / "state" / "approvals.db"


def connect(instance_dir: Path) -> sqlite3.Connection:
    """Open + initialize the approvals DB (idempotent)."""
    path = db_path(instance_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    init(conn)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


def init(conn: sqlite3.Connection) -> None:
    """Apply schema (CREATE IF NOT EXISTS only — safe to call repeatedly)."""
    conn.executescript(SCHEMA)


def init_for_instance(instance_dir: Path) -> None:
    """Open the DB once so the schema lands; close immediately."""
    conn = connect(instance_dir)
    try:
        pass
    finally:
        conn.close()


@contextmanager
def immediate_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """`BEGIN IMMEDIATE` so concurrent deciders serialize on the row."""
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    else:
        conn.execute("COMMIT;")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def row_to_approval(row: sqlite3.Row | None) -> Approval | None:
    if row is None:
        return None
    return Approval(
        approval_id=row["approval_id"],
        kind=row["kind"],
        title=row["title"],
        body=row["body"] or "",
        payload=_loads(row["payload"]),
        status=row["status"],
        requested_at=row["requested_at"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        decision_channel=row["decision_channel"],
        expires_at=row["expires_at"],
        applied_at=row["applied_at"],
        callback_token=row["callback_token"],
        callback_kind=row["callback_kind"],
        callback_payload=_loads(row["callback_payload"]),
        producer=row["producer"],
        source_ref=row["source_ref"],
        notify_telegram=bool(row["notify_telegram"]),
        notify_email=bool(row["notify_email"]),
        notified_at=row["notified_at"],
        result=row["result"],
        schema_version=int(row["schema_version"] or 1),
        note=row["note"] if "note" in row.keys() else None,
        media_paths=tuple(_loads_list(row["media_paths"] if "media_paths" in row.keys() else "[]")),
    )


def _loads(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        out = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _loads_list(value: Any) -> list[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        out = json.loads(value)
    except (TypeError, ValueError):
        return []
    return out if isinstance(out, list) else []
