"""SQLite schema + CRUD for JuliusCaesar workers.

Derived from the memory/db.py pattern: stdlib sqlite3 only, no global state,
every function takes an explicit `instance_dir` Path.

Workers are rows in `state/workers.db`. Each worker's prompt, log, and result
live on disk under `state/workers/<id>/` — the DB row indexes the filesystem.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic           TEXT NOT NULL,
    brain           TEXT NOT NULL,
    model           TEXT,
    prompt_path     TEXT NOT NULL,
    status          TEXT NOT NULL
                        CHECK (status IN ('queued','running','done','failed','cancelled','need_input')),
    pid             INTEGER,
    exit_code       INTEGER,
    log_path        TEXT NOT NULL,
    result_path     TEXT,
    spawned_by      TEXT,
    telegram_msg_id TEXT,
    notify_chat_id  TEXT,
    timeout_seconds INTEGER,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_workers_status ON workers(status);
CREATE INDEX IF NOT EXISTS idx_workers_started ON workers(started_at DESC);
"""


# --- Path helpers ------------------------------------------------------------


def state_dir(instance_dir: Path) -> Path:
    return instance_dir / "state"


def workers_dir(instance_dir: Path) -> Path:
    return state_dir(instance_dir) / "workers"


def db_path(instance_dir: Path) -> Path:
    return state_dir(instance_dir) / "workers.db"


def worker_dir(instance_dir: Path, worker_id: int) -> Path:
    return workers_dir(instance_dir) / str(worker_id)


# --- Dataclass ---------------------------------------------------------------


@dataclass
class Worker:
    id: int
    topic: str
    brain: str
    model: Optional[str]
    prompt_path: str
    status: str
    pid: Optional[int]
    exit_code: Optional[int]
    log_path: str
    result_path: Optional[str]
    spawned_by: Optional[str]
    telegram_msg_id: Optional[str]
    notify_chat_id: Optional[str]
    timeout_seconds: Optional[int]
    started_at: str
    finished_at: Optional[str]
    error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Worker":
        return cls(**{k: row[k] for k in row.keys()})


# --- Connection --------------------------------------------------------------


def connect(instance_dir: Path) -> sqlite3.Connection:
    db_path(instance_dir).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(instance_dir))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# --- Time helper -------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Writes ------------------------------------------------------------------


def create(
    conn: sqlite3.Connection,
    *,
    topic: str,
    brain: str,
    prompt_path: str,
    log_path: str,
    model: Optional[str] = None,
    spawned_by: Optional[str] = None,
    telegram_msg_id: Optional[str] = None,
    notify_chat_id: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
) -> int:
    """Insert a queued worker. Returns the new id."""
    cur = conn.execute(
        """
        INSERT INTO workers (
            topic, brain, model, prompt_path, status,
            log_path, spawned_by, telegram_msg_id, notify_chat_id,
            timeout_seconds, started_at
        ) VALUES (
            :topic, :brain, :model, :prompt_path, 'queued',
            :log_path, :spawned_by, :telegram_msg_id, :notify_chat_id,
            :timeout_seconds, :started_at
        )
        """,
        {
            "topic": topic,
            "brain": brain,
            "model": model,
            "prompt_path": prompt_path,
            "log_path": log_path,
            "spawned_by": spawned_by,
            "telegram_msg_id": telegram_msg_id,
            "notify_chat_id": notify_chat_id,
            "timeout_seconds": timeout_seconds,
            "started_at": _now_iso(),
        },
    )
    conn.commit()
    return int(cur.lastrowid)


def mark_running(conn: sqlite3.Connection, worker_id: int, pid: int) -> None:
    conn.execute(
        "UPDATE workers SET status='running', pid=? WHERE id=?",
        (pid, worker_id),
    )
    conn.commit()


def mark_terminal(
    conn: sqlite3.Connection,
    worker_id: int,
    *,
    status: str,
    exit_code: Optional[int] = None,
    result_path: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Mark worker as done/failed/cancelled/need_input."""
    if status not in ("done", "failed", "cancelled", "need_input"):
        raise ValueError(f"not a terminal status: {status}")
    conn.execute(
        """
        UPDATE workers
        SET status=?, exit_code=?, result_path=?, error=?, finished_at=?, pid=NULL
        WHERE id=?
        """,
        (status, exit_code, result_path, error, _now_iso(), worker_id),
    )
    conn.commit()


# --- Reads -------------------------------------------------------------------


def get(conn: sqlite3.Connection, worker_id: int) -> Optional[Worker]:
    row = conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
    return Worker.from_row(row) if row else None


def list_workers(
    conn: sqlite3.Connection,
    *,
    status: Optional[str] = None,
    limit: int = 20,
) -> list[Worker]:
    if status and status != "all":
        rows = conn.execute(
            "SELECT * FROM workers WHERE status=? ORDER BY started_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM workers ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [Worker.from_row(r) for r in rows]
