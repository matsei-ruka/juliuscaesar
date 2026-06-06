"""§8 — context telemetry.

A normalized `ContextUsage` record per successful invocation and a companion
`session_lifecycle` SQLite table keyed by the session owner key. The router
consumes only `effective_input_tokens`; the provider adapter owns how that
value is derived from raw usage (Anthropic-style cache + input semantics).

The companion table lives in the same `queue.db` as the event queue and the
`sessions` table. It is deliberately separate from `sessions` so provider-
specific telemetry fields can evolve without destabilizing the resume-id row
(§8.3).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class ContextUsage:
    """Normalized usage for one invocation.

    `source` is one of `api`, `native_session`, or `estimate`. The Anthropic
    family reports cache-creation and cache-read tokens separately; the
    effective input is their sum plus the base input tokens (§8.1).
    """

    input_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    output_tokens: int | None
    effective_input_tokens: int | None
    source: str
    measured_at: str

    @classmethod
    def from_anthropic_usage(
        cls,
        usage: dict[str, Any],
        *,
        source: str = "native_session",
        measured_at: str | None = None,
    ) -> "ContextUsage":
        inp = _as_int(usage.get("input_tokens"))
        cache_creation = _as_int(usage.get("cache_creation_input_tokens"))
        cache_read = _as_int(usage.get("cache_read_input_tokens"))
        out = _as_int(usage.get("output_tokens"))
        effective = _sum_effective(inp, cache_creation, cache_read)
        return cls(
            input_tokens=inp,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
            output_tokens=out,
            effective_input_tokens=effective,
            source=source,
            measured_at=measured_at or now_iso(),
        )

    @property
    def is_zero(self) -> bool:
        """A failed/synthetic message with no real measurement.

        Per §8.2 a zero-usage record must not overwrite the last known good
        measurement.
        """
        return not self.effective_input_tokens


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sum_effective(*parts: int | None) -> int | None:
    present = [p for p in parts if p is not None]
    if not present:
        return None
    return sum(present)


@dataclass(frozen=True)
class SessionTelemetry:
    owner_key: str
    brain: str
    last_model: str | None
    context_profile: str | None
    effective_input_tokens: int | None
    usage_source: str | None
    turn_count: int
    rotation_count: int
    last_checkpoint_at: str | None
    last_activity_at: str | None
    maintenance_state: str | None


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS session_lifecycle (
            owner_key TEXT PRIMARY KEY,
            brain TEXT NOT NULL,
            last_model TEXT,
            context_profile TEXT,
            effective_input_tokens INTEGER,
            usage_source TEXT,
            turn_count INTEGER NOT NULL DEFAULT 0,
            rotation_count INTEGER NOT NULL DEFAULT 0,
            last_checkpoint_at TEXT,
            last_activity_at TEXT,
            maintenance_state TEXT,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def _row_to_telemetry(row: sqlite3.Row | None) -> SessionTelemetry | None:
    if row is None:
        return None
    return SessionTelemetry(
        owner_key=row["owner_key"],
        brain=row["brain"],
        last_model=row["last_model"],
        context_profile=row["context_profile"],
        effective_input_tokens=row["effective_input_tokens"],
        usage_source=row["usage_source"],
        turn_count=int(row["turn_count"] or 0),
        rotation_count=int(row["rotation_count"] or 0),
        last_checkpoint_at=row["last_checkpoint_at"],
        last_activity_at=row["last_activity_at"],
        maintenance_state=row["maintenance_state"],
    )


def get_telemetry(conn: sqlite3.Connection, *, owner_key: str) -> SessionTelemetry | None:
    init_db(conn)
    return _row_to_telemetry(
        conn.execute(
            "SELECT * FROM session_lifecycle WHERE owner_key=?",
            (owner_key,),
        ).fetchone()
    )


def record_usage(
    conn: sqlite3.Connection,
    *,
    owner_key: str,
    brain: str,
    usage: ContextUsage,
    model: str | None = None,
    context_profile: str | None = None,
) -> SessionTelemetry:
    """Persist a usage measurement and bump the turn counter.

    A zero-usage record (failed/synthetic provider message) keeps the prior
    `effective_input_tokens` / `usage_source` / `last_model` intact but still
    advances `turn_count` and `last_activity_at` (§8.2).
    """
    init_db(conn)
    existing = get_telemetry(conn, owner_key=owner_key)
    ts = now_iso()
    if usage.is_zero and existing is not None:
        eff = existing.effective_input_tokens
        src = existing.usage_source
        last_model = existing.last_model
        profile = existing.context_profile
    else:
        eff = usage.effective_input_tokens
        src = usage.source
        last_model = model if model is not None else (existing.last_model if existing else None)
        profile = context_profile if context_profile is not None else (
            existing.context_profile if existing else None
        )
    turn_count = (existing.turn_count if existing else 0) + 1
    rotation_count = existing.rotation_count if existing else 0
    last_checkpoint_at = existing.last_checkpoint_at if existing else None
    maintenance_state = existing.maintenance_state if existing else None
    conn.execute(
        """
        INSERT INTO session_lifecycle(
            owner_key, brain, last_model, context_profile, effective_input_tokens,
            usage_source, turn_count, rotation_count, last_checkpoint_at,
            last_activity_at, maintenance_state, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(owner_key) DO UPDATE SET
            brain=excluded.brain,
            last_model=excluded.last_model,
            context_profile=excluded.context_profile,
            effective_input_tokens=excluded.effective_input_tokens,
            usage_source=excluded.usage_source,
            turn_count=excluded.turn_count,
            last_activity_at=excluded.last_activity_at,
            updated_at=excluded.updated_at
        """,
        (
            owner_key,
            brain,
            last_model,
            profile,
            eff,
            src,
            turn_count,
            rotation_count,
            last_checkpoint_at,
            ts,
            maintenance_state,
            ts,
        ),
    )
    conn.commit()
    result = get_telemetry(conn, owner_key=owner_key)
    if result is None:
        raise RuntimeError("failed to read recorded telemetry")
    return result


def record_rotation(
    conn: sqlite3.Connection,
    *,
    owner_key: str,
    new_profile: str | None = None,
) -> None:
    """Bump rotation count + reset measured context after a rotation (§12)."""
    init_db(conn)
    existing = get_telemetry(conn, owner_key=owner_key)
    if existing is None:
        return
    ts = now_iso()
    conn.execute(
        """
        UPDATE session_lifecycle SET
            rotation_count=rotation_count+1,
            effective_input_tokens=NULL,
            usage_source=NULL,
            context_profile=COALESCE(?, context_profile),
            maintenance_state='rotated',
            last_activity_at=?,
            updated_at=?
        WHERE owner_key=?
        """,
        (new_profile, ts, ts, owner_key),
    )
    conn.commit()


def list_telemetry(conn: sqlite3.Connection) -> list[SessionTelemetry]:
    init_db(conn)
    rows = conn.execute(
        "SELECT * FROM session_lifecycle ORDER BY effective_input_tokens DESC"
    ).fetchall()
    return [t for row in rows if (t := _row_to_telemetry(row)) is not None]
