"""Durable SQLite event queue for the JuliusCaesar gateway.

The queue is intentionally local-first: one SQLite database under
<instance>/state/gateway/queue.db. Transactions stay short; brain or channel
work must happen after a claim transaction commits.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 5
DEFAULT_RETRY_BACKOFF_SECONDS = (10, 60, 300)

# Per-claim lease tokens: `locked_by` is `<worker_id>#<12-hex>`, minted fresh
# on every claim. A per-PROCESS worker id alone defeats every
# `status='running' AND locked_by=?` guard when the same process re-claims an
# event after lease loss (stale thread and fresh thread share the id → both
# pass complete/fail/renew and both deliver). The token makes each claim
# generation distinguishable; the worker-id prefix keeps log lines readable.
CLAIM_TOKEN_SEPARATOR = "#"


def mint_claim_token(worker_id: str) -> str:
    return f"{worker_id}{CLAIM_TOKEN_SEPARATOR}{uuid.uuid4().hex[:12]}"


def is_claim_token(locked_by: str | None) -> bool:
    return bool(locked_by) and CLAIM_TOKEN_SEPARATOR in locked_by


@dataclass(frozen=True)
class Event:
    id: int
    source: str
    source_message_id: str | None
    user_id: str | None
    conversation_id: str | None
    content: str
    meta: str | None
    status: str
    received_at: str
    available_at: str
    locked_by: str | None
    locked_until: str | None
    started_at: str | None
    finished_at: str | None
    retry_count: int
    response: str | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.meta:
            try:
                data["meta"] = json.loads(self.meta)
            except json.JSONDecodeError:
                pass
        return data

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def add_seconds(ts: str, seconds: int) -> str:
    if ts.endswith("Z"):
        base = datetime.fromisoformat(ts[:-1] + "+00:00")
    else:
        base = datetime.fromisoformat(ts)
    return (base + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


def queue_dir(instance_dir: Path) -> Path:
    return instance_dir / "state" / "gateway"


def queue_path(instance_dir: Path) -> Path:
    return queue_dir(instance_dir) / "queue.db"


def connect(instance_dir: Path) -> sqlite3.Connection:
    queue_dir(instance_dir).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(queue_path(instance_dir), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    from . import sessions

    sessions.init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        INSERT OR IGNORE INTO meta(key, value)
        VALUES ('schema_version', '4');

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_message_id TEXT,
            user_id TEXT,
            conversation_id TEXT,
            content TEXT NOT NULL,
            meta TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            received_at TEXT NOT NULL,
            available_at TEXT NOT NULL,
            locked_by TEXT,
            locked_until TEXT,
            started_at TEXT,
            finished_at TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            response TEXT,
            error TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup
        ON events(source, source_message_id)
        WHERE source_message_id IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_events_ready
        ON events(status, available_at, id);

        CREATE INDEX IF NOT EXISTS idx_events_lock
        ON events(locked_until);

        CREATE INDEX IF NOT EXISTS idx_events_conversation
        ON events(source, user_id, conversation_id, received_at DESC);

        CREATE TABLE IF NOT EXISTS chats (
            channel          TEXT NOT NULL,
            chat_id          TEXT NOT NULL,
            chat_type        TEXT,
            title            TEXT,
            username         TEXT,
            member_count     INTEGER,
            first_seen       TEXT NOT NULL,
            last_seen        TEXT NOT NULL,
            last_message_id  TEXT,
            auth_status      TEXT NOT NULL DEFAULT 'pending',
            PRIMARY KEY (channel, chat_id)
        );

        CREATE INDEX IF NOT EXISTS idx_chats_last_seen
        ON chats(channel, last_seen DESC);

        CREATE TABLE IF NOT EXISTS deliveries (
            event_id     INTEGER NOT NULL,
            channel      TEXT    NOT NULL,
            status       TEXT    NOT NULL,
            message_id   TEXT,
            locked_by    TEXT,
            attempted_at TEXT    NOT NULL,
            sent_at      TEXT,
            PRIMARY KEY (event_id, channel)
        );
        """
    )
    add_column_if_missing(
        conn,
        table="chats",
        column="auth_status",
        column_ddl="TEXT NOT NULL DEFAULT 'pending'",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chats_auth_status "
        "ON chats(channel, auth_status)"
    )
    conn.execute(
        "UPDATE meta SET value=? WHERE key='schema_version'",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def add_column_if_missing(
    conn: sqlite3.Connection,
    *,
    table: str,
    column: str,
    column_ddl: str,
) -> bool:
    """Idempotent `ALTER TABLE ADD COLUMN` for SQLite (no native IF NOT EXISTS).

    Returns True iff the column was just added.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column in cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_ddl}")
    return True


def row_to_event(row: sqlite3.Row | None) -> Event | None:
    if row is None:
        return None
    return Event(**{key: row[key] for key in row.keys()})


def encode_meta(meta: dict[str, Any] | None) -> str | None:
    if meta is None:
        return None
    return json.dumps(meta, sort_keys=True, separators=(",", ":"))


def enqueue(
    conn: sqlite3.Connection,
    *,
    source: str,
    content: str,
    source_message_id: str | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    meta: dict[str, Any] | None = None,
    available_at: str | None = None,
) -> tuple[Event, bool]:
    """Insert an event.

    Returns (event, inserted). If a source/source_message_id duplicate already
    exists, returns the existing event with inserted=False.
    """

    ts = now_iso()
    available_at = available_at or ts
    meta_text = encode_meta(meta)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO events(
            source, source_message_id, user_id, conversation_id, content, meta,
            status, received_at, available_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
        """,
        (source, source_message_id, user_id, conversation_id, content, meta_text, ts, available_at),
    )
    conn.commit()

    inserted = cur.rowcount == 1
    if inserted:
        row = conn.execute("SELECT * FROM events WHERE id=?", (cur.lastrowid,)).fetchone()
    elif source_message_id is not None:
        row = conn.execute(
            "SELECT * FROM events WHERE source=? AND source_message_id=?",
            (source, source_message_id),
        ).fetchone()
    else:
        row = None
    event = row_to_event(row)
    if event is None:
        raise RuntimeError("failed to read enqueued event")
    return event, inserted


def requeue_expired(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
    max_retries: int | None = None,
) -> list[int]:
    """Move every `running` event whose lease has expired back to `queued`.

    Each expired row's ``retry_count`` is incremented so a poison event that
    repeatedly burns its lease cannot requeue forever. When ``max_retries`` is
    provided, rows whose incremented count would exceed it are routed to
    ``failed`` instead of ``queued`` (mirrors :func:`fail` semantics) and are
    NOT included in the return value.

    Returns the ids of the requeued events (in id order). Caller can `len()`
    for a count, or iterate for log/audit. Snapshot the candidates first so
    the diagnostic log can name the rows that were actually moved — knowing
    `which` events expired is the key signal for debugging dispatch hangs.

    Both UPDATEs re-assert ``status='running' AND locked_until <= ?`` so a
    concurrent ``renew_lease`` between the snapshot SELECT and the UPDATE
    keeps the row instead of losing it (TOCTOU guard).
    """

    now = now or now_iso()
    rows = conn.execute(
        """
        SELECT id, retry_count FROM events
        WHERE status='running'
          AND locked_until IS NOT NULL
          AND locked_until <= ?
        ORDER BY id
        """,
        (now,),
    ).fetchall()
    if not rows:
        return []
    requeue_ids: list[int] = []
    exhausted_ids: list[int] = []
    for row in rows:
        if max_retries is not None and int(row["retry_count"]) + 1 > max_retries:
            exhausted_ids.append(int(row["id"]))
        else:
            requeue_ids.append(int(row["id"]))
    if requeue_ids:
        placeholders = ",".join("?" for _ in requeue_ids)
        conn.execute(
            f"""
            UPDATE events
            SET status='queued',
                retry_count=retry_count+1,
                available_at=?,
                locked_by=NULL,
                locked_until=NULL,
                error=COALESCE(error, 'lease expired')
            WHERE id IN ({placeholders})
              AND status='running'
              AND locked_until <= ?
            """,
            [now, *requeue_ids, now],
        )
    if exhausted_ids:
        placeholders = ",".join("?" for _ in exhausted_ids)
        conn.execute(
            f"""
            UPDATE events
            SET status='failed',
                retry_count=retry_count+1,
                finished_at=?,
                locked_by=NULL,
                locked_until=NULL,
                error=COALESCE(error, 'lease expired (max retries exceeded)')
            WHERE id IN ({placeholders})
              AND status='running'
              AND locked_until <= ?
            """,
            [now, *exhausted_ids, now],
        )
    return requeue_ids


def renew_lease(
    conn: sqlite3.Connection,
    event_ids: int | Iterable[int],
    *,
    worker_id: str,
    lease_seconds: int = 300,
) -> int:
    """Bump ``locked_until`` forward for events still owned by ``worker_id``.

    Returns the number of rows whose lease was successfully extended. A return
    of ``0`` means the worker has lost the lease (event already requeued by
    ``requeue_expired`` and re-claimed elsewhere, or completed). Callers should
    treat ``0`` as a signal to stop heartbeating and abandon any pending write.

    The guard is ``status='running' AND locked_by=?`` — identical to the guard
    used by ``complete`` and ``fail`` (Bug #4) — so a stale worker cannot bump
    the lease of a row that a fresh claimant now owns.
    """

    if isinstance(event_ids, int):
        ids: list[int] = [event_ids]
    else:
        ids = [int(eid) for eid in event_ids]
    if not ids:
        return 0
    now = now_iso()
    new_until = add_seconds(now, lease_seconds)
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"""
        UPDATE events
        SET locked_until=?
        WHERE id IN ({placeholders})
          AND status='running'
          AND locked_by=?
        """,
        (new_until, *ids, worker_id),
    )
    conn.commit()
    return cur.rowcount


def claim_next(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    lease_seconds: int = 300,
    sources: Iterable[str] | None = None,
    max_retries: int | None = None,
) -> Event | None:
    """Claim the next ready event with a short SQLite write transaction.

    ``locked_by`` is set to a fresh per-claim token (``mint_claim_token``),
    NOT the bare ``worker_id``. Callers must use the returned event's
    ``.locked_by`` for ``expected_locked_by`` guards and lease renewal.

    ``max_retries`` is forwarded to the inline ``requeue_expired`` so a
    poison row whose lease expired is escalated to ``failed`` *before* the
    claim SELECT runs — without it, the expired row flips back to ``queued``
    and is re-claimed in this same transaction, forever (the runtime's
    periodic requeue tick never sees it).
    """

    now = now_iso()
    locked_until = add_seconds(now, lease_seconds)
    claim_token = mint_claim_token(worker_id)
    source_list = list(sources or [])
    params: list[Any] = [now]
    source_clause = ""
    if source_list:
        source_clause = f" AND source IN ({','.join('?' for _ in source_list)})"
        params.extend(source_list)

    try:
        conn.execute("BEGIN IMMEDIATE")
        requeue_expired(conn, now=now, max_retries=max_retries)
        row = conn.execute(
            f"""
            SELECT id FROM events
            WHERE status='queued'
              AND available_at <= ?
              {source_clause}
            ORDER BY id
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            conn.commit()
            return None

        event_id = int(row["id"])
        conn.execute(
            """
            UPDATE events
            SET status='running',
                locked_by=?,
                locked_until=?,
                started_at=COALESCE(started_at, ?),
                error=NULL
            WHERE id=?
            """,
            (claim_token, locked_until, now, event_id),
        )
        event = row_to_event(conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())
        conn.commit()
        return event
    except Exception:
        conn.rollback()
        raise


def claim_batch_same_conversation(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    lease_seconds: int = 300,
    sources: Iterable[str] | None = None,
    max_retries: int | None = None,
) -> list[Event]:
    """Claim the oldest queued event and every other queued event sharing its
    `conversation_id`. Returned events are ordered by id ascending.

    If the oldest claimable event has a NULL `conversation_id`, only that
    single event is claimed (NULL doesn't equal NULL in SQL, and unrelated
    NULL-conv events must not be batched together).

    Empty list when nothing is claimable. All rows in the batch share one
    fresh per-claim token in ``locked_by`` (see ``mint_claim_token``).
    ``max_retries``: see :func:`claim_next`.
    """

    now = now_iso()
    locked_until = add_seconds(now, lease_seconds)
    claim_token = mint_claim_token(worker_id)
    source_list = list(sources or [])
    params: list[Any] = [now]
    source_clause = ""
    if source_list:
        source_clause = f" AND source IN ({','.join('?' for _ in source_list)})"
        params.extend(source_list)

    try:
        conn.execute("BEGIN IMMEDIATE")
        requeue_expired(conn, now=now, max_retries=max_retries)
        head = conn.execute(
            f"""
            SELECT id, conversation_id FROM events
            WHERE status='queued'
              AND available_at <= ?
              {source_clause}
            ORDER BY id
            LIMIT 1
            """,
            params,
        ).fetchone()
        if head is None:
            conn.commit()
            return []

        head_id = int(head["id"])
        conv_id = head["conversation_id"]

        if conv_id is None:
            ids = [head_id]
        else:
            batch_params: list[Any] = [conv_id, now]
            batch_source_clause = ""
            if source_list:
                batch_source_clause = (
                    f" AND source IN ({','.join('?' for _ in source_list)})"
                )
                batch_params.extend(source_list)
            rows = conn.execute(
                f"""
                SELECT id FROM events
                WHERE status='queued'
                  AND conversation_id=?
                  AND available_at <= ?
                  {batch_source_clause}
                ORDER BY id
                """,
                batch_params,
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if head_id not in ids:
                ids = sorted({head_id, *ids})

        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"""
            UPDATE events
            SET status='running',
                locked_by=?,
                locked_until=?,
                started_at=COALESCE(started_at, ?),
                error=NULL
            WHERE id IN ({placeholders})
            """,
            (claim_token, locked_until, now, *ids),
        )
        event_rows = conn.execute(
            f"SELECT * FROM events WHERE id IN ({placeholders}) ORDER BY id",
            ids,
        ).fetchall()
        conn.commit()
        return [event for row in event_rows if (event := row_to_event(row)) is not None]
    except Exception:
        conn.rollback()
        raise


def owned_count(
    conn: sqlite3.Connection,
    event_ids: Iterable[int],
    *,
    locked_by: str,
) -> int:
    """Count how many of ``event_ids`` are still ``running`` under ``locked_by``.

    Pre-delivery ownership gate: a worker about to send a response checks
    that every row of its claim is still its own. After a lease loss +
    re-claim the row carries a *different* claim token (or NULL), so the
    stale worker sees a count below ``len(event_ids)`` and must skip
    delivery — the fresh claimant is the one that replies.
    """
    ids = [int(eid) for eid in event_ids]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM events
        WHERE id IN ({placeholders})
          AND status='running'
          AND locked_by=?
        """,
        (*ids, locked_by),
    ).fetchone()
    return int(row["n"])


def begin_delivery(
    conn: sqlite3.Connection,
    *,
    event_id: int,
    channel: str,
    locked_by: str,
) -> tuple[str, str | None]:
    """Reserve the right to send the reply for ``(event_id, channel)``.

    Outbound idempotency ledger (audit Phase 2 #1/#2 — duplicate replies).
    Delivery happens BEFORE ``complete()``; a crash (or lease loss) between
    the channel send and the status flip leaves the row ``running``, so the
    re-claim re-runs the brain and would send a second reply. The ledger row
    is the durable delivered-marker that survives process restarts.

    Returns ``(verdict, message_id)``:
      - ``("proceed", None)`` — no prior attempt (row inserted as
        ``sending``), or a ``sending`` row from THIS claim (same-claim
        re-entry). Caller sends, then calls :func:`finish_delivery` on
        success or :func:`clear_delivery` on a provably-undelivered failure.
      - ``("already_sent", message_id)`` — a previous claim confirmed the
        send. Skip the send; complete the event with the stored id.
      - ``("ambiguous", None)`` — a ``sending`` row from a DIFFERENT claim:
        that attempt crashed mid-send or timed out post-accept, outcome
        unknown. At-most-once: skip the send and complete. A possibly-lost
        reply beats a duplicate.
    """
    ts = now_iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, message_id, locked_by FROM deliveries "
            "WHERE event_id=? AND channel=?",
            (event_id, channel),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO deliveries(event_id, channel, status, locked_by, attempted_at)
                VALUES (?, ?, 'sending', ?, ?)
                """,
                (event_id, channel, locked_by, ts),
            )
            conn.commit()
            return ("proceed", None)
        conn.commit()
        if row["status"] == "sent":
            return ("already_sent", row["message_id"])
        if row["locked_by"] == locked_by:
            return ("proceed", None)
        return ("ambiguous", None)
    except Exception:
        conn.rollback()
        raise


def finish_delivery(
    conn: sqlite3.Connection,
    *,
    event_id: int,
    channel: str,
    message_id: str | None,
) -> None:
    """Confirm the send for ``(event_id, channel)`` (``sending`` → ``sent``)."""
    conn.execute(
        """
        UPDATE deliveries
        SET status='sent', message_id=?, sent_at=?
        WHERE event_id=? AND channel=?
        """,
        (message_id, now_iso(), event_id, channel),
    )
    conn.commit()


def clear_delivery(
    conn: sqlite3.Connection,
    *,
    event_id: int,
    channel: str,
    locked_by: str,
) -> bool:
    """Release a reservation after a PROVABLY-undelivered send failure.

    Only the claim that made the reservation may clear it, and only while it
    is still ``sending`` — a ``sent`` row is permanent. Returns True iff the
    row was removed (a later retry may then deliver).

    Callers must NOT clear after an ambiguous failure (exception that may
    have fired post-accept): the surviving ``sending`` row is what blocks
    the duplicate.
    """
    cur = conn.execute(
        """
        DELETE FROM deliveries
        WHERE event_id=? AND channel=? AND status='sending' AND locked_by=?
        """,
        (event_id, channel, locked_by),
    )
    conn.commit()
    return cur.rowcount > 0


def delivery_record(
    conn: sqlite3.Connection,
    *,
    event_id: int,
    channel: str,
) -> sqlite3.Row | None:
    """Read the ledger row for ``(event_id, channel)`` (tests / doctor)."""
    return conn.execute(
        "SELECT * FROM deliveries WHERE event_id=? AND channel=?",
        (event_id, channel),
    ).fetchone()


def complete(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    response: str = "",
    expected_locked_by: str | None = None,
) -> Event:
    """Mark an event done.

    When ``expected_locked_by`` is provided, the UPDATE is guarded by
    ``status='running' AND locked_by=?`` so a stale worker whose lease has
    already been re-claimed by someone else cannot clobber the new claimant's
    row (Bug #4). When the guard rejects the write, ``KeyError`` is raised so
    the caller knows the row no longer belongs to it.

    Without the kwarg the call is unguarded (legacy fixture path).
    """
    ts = now_iso()
    if expected_locked_by is not None:
        cur = conn.execute(
            """
            UPDATE events
            SET status='done',
                finished_at=?,
                locked_by=NULL,
                locked_until=NULL,
                response=?,
                error=NULL
            WHERE id=? AND status='running' AND locked_by=?
            """,
            (ts, response, event_id, expected_locked_by),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise KeyError(event_id)
    else:
        conn.execute(
            """
            UPDATE events
            SET status='done',
                finished_at=?,
                locked_by=NULL,
                locked_until=NULL,
                response=?,
                error=NULL
            WHERE id=?
            """,
            (ts, response, event_id),
        )
        conn.commit()
    event = row_to_event(conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())
    if event is None:
        raise KeyError(event_id)
    return event


def fail(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    error: str,
    max_retries: int = 3,
    backoff_seconds: tuple[int, ...] = DEFAULT_RETRY_BACKOFF_SECONDS,
    expected_locked_by: str | None = None,
) -> Event:
    """Mark an event failed (or requeue for retry).

    When ``expected_locked_by`` is provided, the UPDATE is guarded by
    ``status='running' AND locked_by=?`` so a stale worker cannot clobber a
    freshly-claimed row (Bug #4). On guard rejection ``KeyError`` is raised.

    Without the kwarg the call is unguarded (legacy fixture path).
    """
    row = conn.execute(
        "SELECT retry_count, status, locked_by FROM events WHERE id=?",
        (event_id,),
    ).fetchone()
    if row is None:
        raise KeyError(event_id)
    if expected_locked_by is not None:
        if row["status"] != "running" or row["locked_by"] != expected_locked_by:
            raise KeyError(event_id)

    retry_count = int(row["retry_count"]) + 1
    ts = now_iso()
    if retry_count <= max_retries:
        delay = backoff_seconds[min(retry_count - 1, len(backoff_seconds) - 1)]
        if expected_locked_by is not None:
            cur = conn.execute(
                """
                UPDATE events
                SET status='queued',
                    retry_count=?,
                    available_at=?,
                    locked_by=NULL,
                    locked_until=NULL,
                    error=?
                WHERE id=? AND status='running' AND locked_by=?
                """,
                (retry_count, add_seconds(ts, delay), error, event_id, expected_locked_by),
            )
            conn.commit()
            if cur.rowcount == 0:
                raise KeyError(event_id)
        else:
            conn.execute(
                """
                UPDATE events
                SET status='queued',
                    retry_count=?,
                    available_at=?,
                    locked_by=NULL,
                    locked_until=NULL,
                    error=?
                WHERE id=?
                """,
                (retry_count, add_seconds(ts, delay), error, event_id),
            )
            conn.commit()
    else:
        if expected_locked_by is not None:
            cur = conn.execute(
                """
                UPDATE events
                SET status='failed',
                    retry_count=?,
                    finished_at=?,
                    locked_by=NULL,
                    locked_until=NULL,
                    error=?
                WHERE id=? AND status='running' AND locked_by=?
                """,
                (retry_count, ts, error, event_id, expected_locked_by),
            )
            conn.commit()
            if cur.rowcount == 0:
                raise KeyError(event_id)
        else:
            conn.execute(
                """
                UPDATE events
                SET status='failed',
                    retry_count=?,
                    finished_at=?,
                    locked_by=NULL,
                    locked_until=NULL,
                    error=?
                WHERE id=?
                """,
                (retry_count, ts, error, event_id),
            )
            conn.commit()
    event = row_to_event(conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())
    if event is None:
        raise KeyError(event_id)
    return event


def retry_now(conn: sqlite3.Connection, event_id: int) -> Event:
    ts = now_iso()
    cur = conn.execute(
        """
        UPDATE events
        SET status='queued',
            available_at=?,
            locked_by=NULL,
            locked_until=NULL,
            finished_at=NULL,
            error=NULL
        WHERE id=?
        """,
        (ts, event_id),
    )
    if cur.rowcount != 1:
        raise KeyError(event_id)
    conn.commit()
    event = get(conn, event_id)
    if event is None:
        raise KeyError(event_id)
    return event


def reset_running_to_queued(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    drop_resume_session: bool = False,
    available_in_seconds: int = 0,
    expected_locked_by: str | None = None,
) -> bool:
    """Reset a 'running' event back to 'queued' (supervisor silent recovery).

    Returns True if the row was actually transitioned. Refuses to reset rows
    not in 'running' status, and — when ``expected_locked_by`` is provided —
    refuses to reset rows whose ``locked_by`` has changed since the supervisor
    snapshot. Compare-and-set guards Bug #2: between the supervisor's snapshot
    read and this UPDATE, a lease may have expired and the dispatcher may have
    re-claimed the event to a *different* worker; without the CAS we would
    yank the row out from under the new worker.

    The whole operation runs inside ``BEGIN IMMEDIATE`` so dispatcher claims
    (which also acquire a write lock) cannot interleave with the SELECT.

    Optional:
      - drop_resume_session: removes ``resume_session`` from meta JSON
        (used for session-poison class).
      - available_in_seconds: backoff before dispatcher re-picks the event.
      - expected_locked_by: when set, only reset if ``locked_by`` matches.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, meta, locked_by FROM events WHERE id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise KeyError(event_id)
        if row["status"] != "running":
            conn.rollback()
            return False
        if (
            expected_locked_by is not None
            and row["locked_by"] != expected_locked_by
        ):
            conn.rollback()
            return False

        new_available_at = add_seconds(now_iso(), available_in_seconds)

        if drop_resume_session:
            try:
                meta = json.loads(row["meta"]) if row["meta"] else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            if isinstance(meta, dict) and "resume_session" in meta:
                meta.pop("resume_session", None)
            meta_text = (
                encode_meta(meta) if isinstance(meta, dict) else row["meta"]
            )
            if expected_locked_by is not None:
                cur = conn.execute(
                    """
                    UPDATE events
                    SET status='queued',
                        available_at=?,
                        locked_by=NULL,
                        locked_until=NULL,
                        started_at=NULL,
                        meta=?
                    WHERE id=? AND status='running' AND locked_by=?
                    """,
                    (new_available_at, meta_text, event_id, expected_locked_by),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE events
                    SET status='queued',
                        available_at=?,
                        locked_by=NULL,
                        locked_until=NULL,
                        started_at=NULL,
                        meta=?
                    WHERE id=? AND status='running'
                    """,
                    (new_available_at, meta_text, event_id),
                )
        else:
            if expected_locked_by is not None:
                cur = conn.execute(
                    """
                    UPDATE events
                    SET status='queued',
                        available_at=?,
                        locked_by=NULL,
                        locked_until=NULL,
                        started_at=NULL
                    WHERE id=? AND status='running' AND locked_by=?
                    """,
                    (new_available_at, event_id, expected_locked_by),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE events
                    SET status='queued',
                        available_at=?,
                        locked_by=NULL,
                        locked_until=NULL,
                        started_at=NULL
                    WHERE id=? AND status='running'
                    """,
                    (new_available_at, event_id),
                )
        if cur.rowcount == 0:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def mark_event_failed(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    error: str = "recovery_escalated",
) -> bool:
    """Mark a 'running' event as failed (supervisor Phase 6 escalation).

    Called after ``max_recovery_attempts`` is exhausted. Only transitions
    rows in 'running' status — refuses to clobber already-terminal rows.
    Returns True if the row was actually transitioned.
    """
    row = conn.execute(
        "SELECT status FROM events WHERE id=?", (event_id,)
    ).fetchone()
    if row is None:
        raise KeyError(event_id)
    if row["status"] != "running":
        return False
    conn.execute(
        """UPDATE events
           SET status='failed', finished_at=?, locked_by=NULL, locked_until=NULL, error=?
           WHERE id=? AND status='running'""",
        (now_iso(), error, event_id),
    )
    conn.commit()
    return True


def update_meta(conn: sqlite3.Connection, event_id: int, meta: dict[str, Any]) -> Event:
    cur = conn.execute(
        "UPDATE events SET meta=? WHERE id=?",
        (encode_meta(meta), event_id),
    )
    if cur.rowcount != 1:
        raise KeyError(event_id)
    conn.commit()
    event = get(conn, event_id)
    if event is None:
        raise KeyError(event_id)
    return event


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM events GROUP BY status").fetchall()
    return {str(row["status"]): int(row["n"]) for row in rows}


def recent(conn: sqlite3.Connection, *, limit: int = 20) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [event for row in rows if (event := row_to_event(row)) is not None]


def get(conn: sqlite3.Connection, event_id: int) -> Event | None:
    return row_to_event(conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())
