# Telegram chat discovery — track all chats, surface in preamble

**Status:** Spec
**Author:** Rachel
**Date:** 2026-04-26
**Branch:** `feat/telegram-chat-discovery`

## Problem

Rachel currently has no durable record of which Telegram chats she has been
added to. Each inbound message ships a `chat_id` in `meta`, but the moment
the event finishes processing that knowledge is gone. Concretely:

1. **No directory of chats.** If Luca asks "what groups am I in with you?"
   or "DM that Cardcentric chat," Rachel has to guess. The data is on
   Telegram's servers but not in the gateway DB.
2. **No preamble awareness.** The brain receives the active
   `conversation_id` per event, but never learns the *set* of chats she
   inhabits. That blocks any reasoning that spans chats ("post the same
   thing to both BNESIM groups," "is this a new chat or one I already
   know?").
3. **Group metadata churns silently.** Title renames, member-count changes,
   chat-type promotions (group → supergroup) all happen on Telegram's side
   with no audit trail in our state. After-the-fact debugging — "why did we
   stop seeing messages from chat X" — has no historical record to consult.
4. **No CLI.** Even when Luca knows a chat exists, there is no way to list
   chats from the shell. Every other gateway-resident object (events,
   sessions, workers, memory entries) has a `jc <noun>` listing command;
   chats are the gap.

## Goals

- Persist every Telegram chat the bot sees into a new `chats` table inside
  `state/gateway/queue.db`, upserted on every inbound message.
- Surface the chat directory inside the prompt preamble so the brain knows
  the full chat universe on every turn (not only the active chat).
- Add `jc chats list` (and `jc chats show <chat_id>`) CLI.
- Spec-first per the repo rule. No code shipped until Luca has signed off
  on this document.

## Non-goals

- Tracking individual users or per-chat member rosters. We record
  `member_count` only — full membership is a privacy concern and a
  Telegram-API rate-limit concern. Out of scope.
- Backfilling chats from history. The table starts empty; rows appear as
  messages arrive. A future `jc chats backfill` could iterate
  `getUpdates` history, but not in this spec.
- Cross-channel parity. This spec is Telegram-only. The schema is designed
  to extend (`channel` column), but Discord/Slack/etc. is not implemented
  here.
- Outbound-side updates. We do not modify `send()` to record chats Rachel
  is *sending* to but has never *received* from. In practice every active
  chat receives at some point, so the gap is theoretical.

## Schema

New table in `lib/gateway/queue.py:init_db`. Schema version bumps `2 → 3`.

```sql
CREATE TABLE IF NOT EXISTS chats (
    channel          TEXT NOT NULL,
    chat_id          TEXT NOT NULL,
    chat_type        TEXT,         -- private | group | supergroup | channel
    title            TEXT,         -- chat title (groups) or first+last (DMs)
    username         TEXT,         -- @handle if any
    member_count     INTEGER,      -- last observed; NULL for DMs
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    last_message_id  TEXT,         -- Telegram message_id of most recent inbound
    PRIMARY KEY (channel, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_chats_last_seen
ON chats(channel, last_seen DESC);
```

**Why composite PK `(channel, chat_id)` instead of `chat_id` alone:**
keeps the schema portable when a future Discord/Slack adapter writes here.
Telegram chat_ids are integers; Discord channel-ids are 64-bit snowflakes;
overlap is statistically impossible but architecturally inevitable.

**Why `username` separate from `title`:** group titles are free-form and
mutable; the `@handle` is what users actually type. Both are needed for
display.

**Why store `last_message_id`:** lets a future feature (e.g. "summarize
messages I missed in chat X") seek into Telegram's `getUpdates` from a
known starting point.

**Why no `bot_added_at` / `bot_removed_at`:** out of scope — first_seen
already approximates "when the bot first heard from this chat," and
removal is detectable by `last_seen` ageing out. If Luca later wants
explicit join/leave events, we can add a `chat_events` table.

## Code changes

### `lib/gateway/queue.py`

- Add `CHATS_SCHEMA` block to `init_db`.
- Bump `SCHEMA_VERSION` constant from `2` to `3`.
- Add a migration step: if the existing `meta.schema_version == '2'`, run
  the `CREATE TABLE chats` DDL idempotently (it already is, via `IF NOT
  EXISTS`), then `UPDATE meta SET value='3'`. No data migration needed —
  table starts empty.

### `lib/gateway/chats.py` (new)

Thin module mirroring `lib/gateway/sessions.py`:

```python
def upsert_chat(
    instance_dir: Path,
    *,
    channel: str,
    chat_id: str,
    chat_type: str | None,
    title: str | None,
    username: str | None,
    member_count: int | None,
    last_message_id: str | None,
) -> None:
    """Upsert a chat row, refreshing last_seen and any non-null fields."""

def list_chats(
    instance_dir: Path,
    *,
    channel: str | None = None,
    limit: int | None = None,
) -> list[Chat]:
    """List chats, ordered by last_seen DESC."""

def get_chat(
    instance_dir: Path,
    *,
    channel: str,
    chat_id: str,
) -> Chat | None:
    ...
```

The upsert uses `INSERT ... ON CONFLICT(channel, chat_id) DO UPDATE`,
preserves `first_seen`, refreshes `last_seen`, and only overwrites
`title`/`username`/`member_count`/`chat_type` when the new value is
non-NULL — so a momentarily missing field in one update doesn't wipe a
previously-known value.

### `lib/gateway/channels/telegram.py`

In `run()` after `self._log_forward(message, update_id)` (line 228),
before the text/media extraction, call:

```python
self._record_chat(chat, message)
```

New helper `_record_chat(chat: dict, message: dict) -> None`:

- `channel = "telegram"`
- `chat_id = str(chat["id"])`
- `chat_type = chat.get("type")`
- `title = chat.get("title") or self._dm_title(chat)`
  (DMs: `f"{first_name} {last_name}".strip()` falling back to `username`)
- `username = chat.get("username")`
- `member_count = self._get_cached_member_count(chat_id)`
  (reuses `_member_count_cache` from telegram-group-context — no extra
  HTTP calls)
- `last_message_id = str(message.get("message_id"))`
- Calls `upsert_chat(self.instance_dir, ...)`

Failure mode: the upsert is wrapped in `try/except` and only logs on
failure. Chat tracking is observability — must never block message
processing.

### `lib/gateway/brains/base.py`

Modify `prompt_for_event` to inject a `## Known Telegram chats` section
between the L1 preamble and the `# Incoming event` block, but only when:

- `event.source == "telegram"`, AND
- `self.needs_l1_preamble is True` (i.e. for non-Claude brains; Claude gets
  L1 from `CLAUDE.md` auto-discovery and we'll surface chats there
  separately, see next section).

Format (kept compact — this section runs every event):

```
## Known Telegram chats

- 123456 | private | Luca Mattei (@luca_mattei) — last 2026-04-26 09:00
- -100789 | supergroup | BNESIM ops (5 members) — last 2026-04-26 08:42
...
```

Cap at 20 most-recent rows to keep the preamble bounded. Querying 20 rows
adds <1ms; well below the lease budget.

### Claude brain — preamble via L1 file

Claude doesn't pass through `prompt_for_event`'s preamble (the `claude`
CLI auto-loads `CLAUDE.md` instead). So for Claude, we inject chat
awareness via a **generated L1 file**: `memory/L1/CHATS.md`, regenerated
on every chat upsert (debounced — at most once per 30 s) by `chats.py`.

`CLAUDE.md` already imports `@memory/L1/*.md`, but only the four files
listed (IDENTITY/USER/RULES/HOT). We add a fifth import line in the
instance template's `CLAUDE.md` and let existing instances opt in:

```markdown
@memory/L1/IDENTITY.md
@memory/L1/USER.md
@memory/L1/RULES.md
@memory/L1/HOT.md
@memory/L1/CHATS.md   # new — auto-generated from queue.db chats table
```

Rachel's instance gets this added in the same PR (the spec is for the
framework; Rachel's `CLAUDE.md` lives in `rachel_zane/`, so two PRs:
JuliusCaesar PR + Rachel instance PR).

The file gets a leading note: `<!-- AUTO-GENERATED — do not edit; rebuilt
from gateway queue.db -->`. `jc memory rebuild` skips it (filename match).

### `bin/jc-chats` (new)

Mirrors `bin/jc-workers`. Subcommands:

- `jc chats list [--channel telegram] [--limit 20]` — table format:
  `chat_id | type | title | members | last_seen`. Sorted by `last_seen
  DESC`.
- `jc chats show <chat_id>` — full row dump including first_seen,
  username, last_message_id.
- `jc chats prune --older-than 90d` — delete rows whose `last_seen` is
  older than threshold. Confirms before deleting unless `--yes`.

Add `chats` to the dispatcher list in `bin/jc` (alongside `memory`,
`workers`, etc.).

### Tests

- `tests/test_chats.py` — `upsert_chat` first-seen vs. updated-seen paths;
  null-coalescing of optional fields; `list_chats` ordering and limit.
- `tests/test_telegram_chat_recording.py` — feed a synthetic
  `getUpdates` payload through the channel's message-handling code
  path, assert the row appears in the DB with the expected fields. DMs
  vs. groups vs. supergroups vs. channels each get a case.
- `tests/test_chat_preamble.py` — synthesize a queue with three known
  chats, assert `prompt_for_event` for a telegram event includes the
  `## Known Telegram chats` section with all three rows.
- `tests/test_jc_chats_cli.py` — invoke `jc-chats list --json`, assert
  output schema.

## Migration

Schema version `2 → 3`. Existing instances on schema 2 pick up the new
table on next `connect()` (DDL is idempotent). The `meta.schema_version`
update is best-effort; if the migration step throws, the daemon still
starts and the table is still created — `init_db` is fail-open by design.

No risk of breaking existing instances:
- The `chats` table is additive; nothing reads from it that didn't exist
  before.
- `prompt_for_event` only injects the new section when there are rows —
  empty preamble for fresh instances.
- `CLAUDE.md` adding a fifth `@memory/L1/CHATS.md` import is a no-op when
  the file is missing (Claude Code's `@import` syntax silently skips
  missing files; verified via testing in another spec last week).

## Open questions

- **Should `prompt_for_event`'s "Known chats" section be opt-in via a
  config flag?** I lean no — for a one-user instance the cost is trivial
  and the benefit (cross-chat awareness) shows up immediately. For a
  high-fanout future instance with hundreds of chats, we cap at 20 anyway.
- **Should DMs get titled by Telegram username vs. real name?** Spec
  defaults to `first_name + last_name`. If Luca prefers `@handle`, one-line
  change in `_dm_title`.
- **Privacy: do we want to redact DM titles in the preamble?** For
  Rachel's instance this is fine (Luca is the only DM peer of substance).
  For a multi-user public instance, this could leak identity. Not solving
  here — will revisit if/when we ship a public-facing instance.

## Out of scope (parking lot)

- Outbound-only chat tracking (DMs the bot initiated).
- `chat_events` audit log (joins, leaves, title-renames as discrete rows).
- Cross-instance chat sharing (e.g. Sergio reading Rachel's chat list).
- Webhook mode (we're still on long-poll `getUpdates`).

## Plan

1. Land this spec on `feat/telegram-chat-discovery` for Luca review.
2. After approval, implement schema + `chats.py` module + tests.
3. Wire telegram channel upsert + tests.
4. Wire preamble injection + tests.
5. Add `bin/jc-chats` CLI + tests.
6. Add CLAUDE.md template fifth-import + Rachel instance update.
7. PR to `juliuscaesar`. Companion PR to `rachel_zane` for the
   `CLAUDE.md` import line.
