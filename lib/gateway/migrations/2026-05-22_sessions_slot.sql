-- Migration: add `slot` column to sessions, broaden UNIQUE constraint.
--
-- Sessions previously held one row per (channel, conversation_id, brain).
-- Parallel slots (docs/specs/parallel-slots.md) need one row per
-- (channel, conversation_id, brain, slot). Existing rows become slot 0,
-- matching the serial-N=1 default.
--
-- SQLite cannot ALTER an existing UNIQUE constraint, so the table is
-- rebuilt: copy → drop → rename. The rebuild is idempotent — `sessions.py`
-- detects the missing `slot` column at startup and runs this sequence;
-- subsequent boots are no-ops.
--
-- This file documents the migration. The runtime executes the equivalent
-- statements via `gateway.sessions._migrate_add_slot()` so no external
-- migration runner is required.

CREATE TABLE IF NOT EXISTS sessions_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    brain TEXT NOT NULL,
    slot INTEGER NOT NULL DEFAULT 0,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(channel, conversation_id, brain, slot)
);

INSERT INTO sessions_new
    (id, channel, conversation_id, brain, slot, session_id, created_at, updated_at)
SELECT
    id, channel, conversation_id, brain, 0, session_id, created_at, updated_at
FROM sessions;

DROP TABLE sessions;

ALTER TABLE sessions_new RENAME TO sessions;

CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
