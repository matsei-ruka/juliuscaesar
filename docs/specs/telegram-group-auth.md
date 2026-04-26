# Telegram group auth — explicit allow/deny on bot-join

**Status:** Spec
**Author:** Rachel
**Date:** 2026-04-26
**Branch:** `feat/telegram-group-auth`

## Problem

After PR #15 (chat discovery) shipped, the bot now records every chat it
sees — but it still **processes** every group it gets added to. Anyone
who knows the bot's handle can drag it into a group and start spamming
@-mentions. Luca wants explicit per-group authorization: when the bot is
added to a new group, the gateway pauses messages from that group and
asks the main DM to allow or deny.

A second issue surfaced in the post-merge gemini review of PR #15:
`chats.upsert_chat` and `chats.list_chats` open a fresh SQLite
connection on every call. Under WAL mode the channel + dispatcher
threads contend for the write lock, and the per-call `init_db` re-runs
every PRAGMA + DDL. Both call paths run on every inbound Telegram
message; the cost compounds.

This spec bundles both: the connection-pool hotfix and the new
group-auth flow.

## Goals

1. Detect bot membership changes via `my_chat_member` updates and
   message-level `new_chat_members` arrays.
2. Mark new groups `pending` in the `chats` table; suppress message
   processing until the user authorizes.
3. Send an inline-keyboard prompt to the main DM with **Allow** / **Deny**
   buttons that resolve to a `chat_auth` callback.
4. Handle `callback_query` updates so a button tap mutates `auth_status`
   in the DB. Deny → `leaveChat` on the group.
5. Add `jc chats approve <chat_id>` / `jc chats deny <chat_id>` /
   `jc chats list --auth-status pending` for headless control.
6. **Hotfix:** route `chats.upsert_chat` / `chats.list_chats` through a
   shared connection cached on the gateway runtime / channel so the
   gateway opens at most one connection per worker thread for chat ops.

## Non-goals

- Cross-process locking around the L1/CHATS.md regenerator. The gateway
  is single-process today; a future multi-process layout would warrant
  `flock`, but adding it now is dead weight.
- DM PII redaction. Explicit non-goal per PR #15 spec; revisit when /
  if a public-facing instance ships.
- Backfill `my_chat_member` history on first run. New schema column
  defaults to `'allowed'` so every pre-existing row stays processable;
  the auth gate kicks in only for *new* chats observed after the
  feature lands.
- Granular permissions (read-only, mention-only). Allow / deny binary
  for now; revisit if more shades are needed.
- Per-thread auth in supergroups. A supergroup is one auth decision.

## Schema

`SCHEMA_VERSION 3 → 4`. Additive column on `chats`:

```sql
ALTER TABLE chats
ADD COLUMN auth_status TEXT NOT NULL DEFAULT 'allowed';
-- one of: 'allowed' | 'pending' | 'denied'
```

Why default `'allowed'` rather than `'pending'`: pre-existing rows
written by chat-discovery (PR #15) represent chats the bot *already*
serves. Forcing them to `pending` would silently break Luca's existing
group conversations until he tapped Allow on each. Default keeps
behavior unchanged for known chats and only gates new arrivals.

Migration step in `init_db`: idempotent `ALTER TABLE` guarded by a
`PRAGMA table_info(chats)` check (SQLite has no native
`ADD COLUMN IF NOT EXISTS`). On a fresh DB, `CREATE TABLE` already
includes the column.

## Code changes

### `lib/gateway/queue.py`

- Bump `SCHEMA_VERSION` from 3 to 4.
- Append `auth_status TEXT NOT NULL DEFAULT 'allowed'` to the `chats`
  table DDL.
- Add an `add_column_if_missing(conn, table, col, ddl)` helper used by
  `init_db` to apply the alter step on already-migrated databases.

### `lib/gateway/chats.py` (hotfix + auth fields)

Refactor signatures so callers can pass an existing connection, while
keeping the legacy "no conn" entry points working for the CLI path.

```python
def upsert_chat(
    instance_dir: Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    channel: str,
    chat_id: str,
    ...
) -> Chat:
    """Either pass an open `conn` (preferred — no connection churn)
    or `instance_dir` to open a one-shot connection.
    """
    own = conn is None
    if own:
        conn = queue.connect(instance_dir)
    try:
        ...
    finally:
        if own:
            conn.close()
```

`list_chats`, `get_chat`, `prune_chats` get the same treatment.
The fast path (`conn` provided) reuses the channel's connection; the
slow path (CLI tools) opens a one-shot.

Add new helpers:

- `set_auth_status(conn|instance_dir, *, channel, chat_id, status)` —
  flips `auth_status`, updates `last_seen`, commits. No COALESCE; this
  one is intent-revealing.
- `pending_chats(...)` — convenience shortcut for `list_chats` with a
  `WHERE auth_status='pending'` filter (used by the CLI's
  `--auth-status pending`).

### `lib/gateway/channels/telegram.py`

1. **Connection cache**: lazy `_chats_conn` opened on first `_record_chat`
   call. Reused for the channel's lifetime. Closed in a new `close()`
   hook the runtime calls on shutdown. Threading: the Telegram poller
   is single-threaded, so a per-channel connection is safe.

2. **`allowed_updates`**: pass
   `["message","edited_message","my_chat_member","callback_query"]` in
   the `getUpdates` query string. Telegram requires explicit opt-in for
   the latter two.

3. **Bot-join detection**:
   - `my_chat_member` update → `new_chat_member.user.id == bot_user_id`
     and `new_chat_member.status` ∈ {`member`, `administrator`} →
     trigger join flow.
   - Backstop: `message.new_chat_members` array contains the bot user
     id (some clients/legacy chats only emit this).

   Both paths route into a new helper `_handle_bot_added(chat, added_by)`
   that:
   - Upserts the chat with `auth_status='pending'`.
   - Sends the inline-keyboard auth prompt to the main DM
     (`TELEGRAM_CHAT_ID`).

4. **Auth-prompt sender** (`_send_auth_prompt`):

   ```
   🤝 Added to a new chat — approve?

   *BNESIM ops* (supergroup, 8 members)
   chat_id: `-1001234567`
   added by: @some_user

   [✅ Allow] [⛔ Deny]
   ```

   Inline keyboard payload:
   ```json
   {"inline_keyboard": [[
     {"text": "✅ Allow", "callback_data": "chat_auth:allow:-1001234567"},
     {"text": "⛔ Deny",  "callback_data": "chat_auth:deny:-1001234567"}
   ]]}
   ```

5. **`callback_query` handler** (`_handle_callback_query`):
   - Reject if `callback_query.from.id != int(TELEGRAM_CHAT_ID)` (only
     the configured owner may authorize). `answerCallbackQuery` with
     `text="not authorized"` so Telegram clears the spinner.
   - Parse `callback_data` `chat_auth:<allow|deny>:<chat_id>`.
   - Flip `auth_status` via `chats.set_auth_status`.
   - On deny: `leaveChat(chat_id)` (best-effort; log on failure).
   - `editMessageText` to confirm: "✅ Allowed BNESIM ops" /
     "⛔ Denied + left BNESIM ops".
   - `answerCallbackQuery` to clear the loading spinner.

6. **Message gate**: `_should_process_message` keeps its existing
   logic, plus a new check after the chat-type branch — if
   `auth_status` is `pending` or `denied`, return False and log once
   (`telegram ignored unauthorized chat_id=… status=…`). DMs and chats
   on the explicit `cfg.chat_ids` allowlist bypass the auth gate
   (legacy contract).

### `bin/jc-chats`

Three additions:

- `jc chats list --auth-status <allowed|pending|denied>` — filter.
- `jc chats approve <chat_id>` — flip pending/denied → allowed.
- `jc chats deny <chat_id>` — flip allowed/pending → denied. Does NOT
  call `leaveChat`; that's the live channel's job. Warns if the chat
  has not yet been seen.

Both `approve` and `deny` are idempotent: re-running on an
already-set status is a no-op (and prints the current status).

### Tests

- `tests/test_chats.py` — connection-cache path: pass a `conn` to
  `upsert_chat`, assert no extra connections opened (mock
  `queue.connect`).
- `tests/test_chats.py` — `set_auth_status` round-trip; `pending_chats`
  filter.
- `tests/test_chats.py` — concurrency: spawn 5 threads calling
  `upsert_chat` on the same chat_id with shared conn, assert no
  exceptions and a single row.
- `tests/gateway/test_telegram_chat_recording.py` — bot-added via
  `my_chat_member` update marks chat `pending` and sends auth prompt
  (assert outbound HTTP includes `inline_keyboard`).
- `tests/gateway/test_telegram_chat_recording.py` — `callback_query`
  with `chat_auth:allow:…` flips status to allowed, edits the message,
  answers the callback. Same for `deny:…` (also asserts `leaveChat`).
- `tests/gateway/test_telegram_chat_recording.py` —
  `_should_process_message` returns False when `auth_status='pending'`
  for a group not in `cfg.chat_ids`.
- `tests/test_jc_chats_cli.py` — `approve` / `deny` / `list
  --auth-status pending` round-trips.

## Migration

`SCHEMA_VERSION 3 → 4`. The `auth_status` column is added by
`add_column_if_missing` on next `connect()`. Existing rows default to
`'allowed'` so currently-active chats stay processable.

Risk surface:
- **Backwards compat**: all pre-existing groups are `allowed`. No silent
  break.
- **`cfg.chat_ids` (env `TELEGRAM_CHAT_ID`) takes priority** — if a
  chat is on the explicit allowlist, the auth gate is skipped. This
  preserves the existing "Luca's DM is always allowed" contract.
- **First migration window**: between `auth_status` column being added
  and the user tapping Allow on a freshly-joined group, messages from
  that group are dropped. That's the *correct* new default — it's the
  whole point of the feature. Logged once per drop.

## Open questions

- **Auto-leave on deny vs. just-mute?** Spec defaults to `leaveChat`
  on deny — clearer signal to the group's admin and stops the bot from
  accumulating unwanted membership. If Luca prefers "stay but stay
  silent," one-line change in `_handle_callback_query`.
- **Re-prompt on chat-rename?** A renamed group is still the same
  `chat_id`; `auth_status` survives. We could re-prompt if the title
  changes substantially, but title-rename griefing is low-probability.
  Out of scope.
- **Outbound queue for auth prompts** — if the bot is added while the
  gateway is offline, the `my_chat_member` update is delivered when
  the poller catches up. The auth prompt is sent then, in batch order.
  Acceptable.

## Out of scope (parking lot)

- Allow / deny per-user granularity inside a single group.
- Quiet-hours — defer auth prompts during 23:00-07:00 Dubai.
- Auto-deny chats added by users not on a personal allowlist.
- Per-chat brain pinning ("BNESIM ops always uses Claude with model
  X").

## Plan

1. Land this spec on `feat/telegram-group-auth` for Luca review.
2. Hotfix: thread shared connections through `chats.py` + tests.
3. Schema bump 3 → 4 with idempotent `ALTER TABLE` migration.
4. Detect bot-added (`my_chat_member` + `new_chat_members` paths) and
   mark chats `pending`.
5. Send auth prompt with inline keyboard.
6. Handle `callback_query` flips + `editMessageText` + `leaveChat`.
7. Filter unauthorized chats from message processing.
8. CLI: approve / deny / `--auth-status` filter.
9. Tests + PR.
