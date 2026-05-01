# Spec: config-only sender approval

Status: implementing
Branch: `feat/sender-approval-config-only`
Author: Rachel (Claude Opus 4.7)
Date: 2026-04-30

## Motivation

The PR #28 Telegram sender-approval flow stores authorization state in the
`chats` SQLite table (`auth_status` column) and reads from it on every
inbound message. Operator complaint, verbatim:

> the protection is not working. You should not check on sqlite db. delete
> this part. if approved, it finishes in gateway.yaml and .env. If
> rejected, finishes in blocked in gateway. Should be strong, solid.

Goal: move the allowlist + blocklist into config files (`ops/gateway.yaml`
+ `.env`). The sqlite `chats` table stops being authoritative for auth.

## Failure modes of current implementation

Audit of `lib/gateway/channels/telegram.py` + `lib/gateway/queue.py` +
the live `state/gateway/queue.db` on the `rachel_zane` instance found:

### Bug #1 — stale DB column DEFAULT (latent)

Source `queue.py:139` declares `auth_status TEXT NOT NULL DEFAULT 'pending'`.
But the live DB on `rachel_zane` shows:

```
$ sqlite3 state/gateway/queue.db ".schema chats"
auth_status TEXT NOT NULL DEFAULT 'allowed',
```

Reason: SQLite preserves the column DDL recorded when the column was
*first added*. Pre-93d4060, the migration ran `ALTER TABLE … ADD COLUMN
auth_status TEXT NOT NULL DEFAULT 'allowed'`. Commit 93d4060 changed
the source to `'pending'`, but `add_column_if_missing` is a no-op once
the column exists, so existing instances are stuck with the old default.

Today this is *latent* because every insert path (`upsert_chat`) supplies
an explicit `COALESCE(?, 'pending')`. But any future raw-INSERT path
that omits the column would silently fail-open. The DB is one bug-fix
away from auto-allowing every stranger.

### Bug #2 — historical fail-open window

`8130606104` (Luca's secondary `@luca80dxb`) was first seen
`2026-04-29T15:44:33Z` — 13 minutes *before* the security commit
93d4060 landed at 15:57 UTC. That row was inserted with `auth_status`
defaulting to `'allowed'` (DB DEFAULT, since pre-fix `upsert_chat`
didn't pass an explicit value). No approval prompt was ever sent; the
chat was silently auto-authorized. Any stranger who DM'd during the
release window would have been auto-authorized too.

The operator has no audit trail to distinguish "approved by me" from
"auto-allowed before the fix landed."

### Bug #3 — DB rotation = lost approvals (or worse)

The `chats` table doubles as approval store. If `queue.db` is wiped or
rotated (currently nothing does this, but it's plausible — see
`prune_chats`), every approval is lost. The next message from any
known sender is treated as a brand-new pending sender, including the
operator's primary DM if `TELEGRAM_CHAT_ID` env was misconfigured.

Conversely, if approval state lives only in DB, there is no version
control over the allowlist. Operator cannot audit history, diff
"who is allowed today vs last week," or restore state by checking out
an older commit.

### Bug #4 — split-brain between yaml `chat_ids` and DB

`channels.telegram.chat_ids` in yaml is one allowlist. `auth_status`
in DB is another. Operator's manual edits to yaml don't appear in DB,
and approvals via inline button don't appear in yaml. Two sources of
truth = drift = surprises.

### What's NOT a bug

- The current `_is_authorized` check itself is correct after 93d4060
  (default-deny on unknown chat, fail-closed on lookup error).
- The `_record_chat` → `_is_authorized` ordering is fine (record-then-check
  means the audit trail captures unauthorized attempts).

The architecture, not the logic, is the issue.

## New architecture

### Single decision tree

```
_is_authorized(chat_id):
    config = reload_if_changed()
    if chat_id in config.blocked_chat_ids: return False    # blocklist wins
    if chat_id in config.allowed_chat_ids: return True
    if chat_id == config.main_chat_id:    return True       # operator DM
    return False                                            # default deny
```

`config.allowed_chat_ids` = union of:
- `channels.telegram.chat_ids` (yaml — already exists, kept)
- `TELEGRAM_CHAT_IDS` from `.env` (new, comma-separated)

`config.blocked_chat_ids` = `channels.telegram.blocked_chat_ids` (yaml — new).

`config.main_chat_id` = `TELEGRAM_CHAT_ID` from `.env` (existing).

Order matters: blocklist is consulted first so an operator can revoke
a previously-approved chat by adding to `blocked_chat_ids` even if
`chat_ids` still mentions it.

No SQLite call on the auth path. Period.

### Approval flow (Allow tap)

1. Operator taps `✅ Allow` on the inline keyboard.
2. `_handle_callback_query` decodes `chat_auth:allow:<chat_id>`.
3. Atomic write to `ops/gateway.yaml`:
   - Add `<chat_id>` to `channels.telegram.chat_ids` if missing.
   - Remove `<chat_id>` from `channels.telegram.blocked_chat_ids` if
     present (re-allowing a previously-blocked chat).
4. Atomic write to `.env`:
   - Add `<chat_id>` to `TELEGRAM_CHAT_IDS` (comma-separated). If the
     var is missing, create it with just this id.
5. Edit the prompt message to `✅ Allowed — <title>`.
6. Mark the in-memory cache dirty so the next poll iteration reloads
   config.

### Rejection flow (Deny tap)

1. Operator taps `⛔ Deny + leave`.
2. Decode `chat_auth:deny:<chat_id>`.
3. Atomic write to `ops/gateway.yaml`:
   - Add `<chat_id>` to `channels.telegram.blocked_chat_ids` if missing.
   - Remove `<chat_id>` from `channels.telegram.chat_ids` if present.
4. `.env` is *not* touched (rejection lives in yaml only, per spec).
5. Call `_leave_chat(chat_id)` (telegram leaveChat).
6. Edit prompt to `⛔ Denied + left — <title>`.

Idempotency: re-tapping the same button is safe. Approving an already-
approved chat is a no-op (set membership). Likewise for denial.

### Drop on poll

In the poll loop, before any other processing:

```
if chat_id in config.blocked_chat_ids:
    self.log(f"telegram dropped blocked chat_id={chat_id}")
    continue
```

This short-circuits before `_record_chat` and `_is_authorized` so
blocked traffic doesn't even touch the audit DB.

### Config file formats

#### `ops/gateway.yaml`

```yaml
channels:
  telegram:
    enabled: true
    token_env: TELEGRAM_BOT_TOKEN
    chat_ids: [28547271, -5174372991, -1003863022337]
    blocked_chat_ids: [-5222293802]
```

`blocked_chat_ids` is a new optional field on `ChannelConfig`. Default
empty tuple. Validated by `_validate_raw_config` (must be string or
list of int-coercible scalars).

#### `.env`

```
TELEGRAM_CHAT_ID='28547271'             # operator's main DM (existing)
TELEGRAM_CHAT_IDS='28547271,-5074463934'  # allowlist (new)
```

`TELEGRAM_CHAT_IDS` is parsed by `env_value` then split on commas.
Whitespace stripped. Empty entries dropped.

### Atomic writes

Both files written via the same helper:

```python
def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
```

Single-machine `os.replace` is atomic on POSIX; a crash mid-write
leaves either the original or the new file, never a partial.

For `.env` we read-modify-write the whole file: parse with the
existing `parse_env_file` flow, splice in/out the `TELEGRAM_CHAT_IDS`
line, preserve everything else byte-for-byte. The naive "rewrite the
file" path would clobber comments and ordering — instead we
line-replace the single line in place when present, append at end
when absent.

For `ops/gateway.yaml` we use a small dedicated emitter that round-
trips through `_load_raw` → mutate the dict → re-emit. This loses
comments. Mitigation: only write when we have to (after a tap), and
log loudly so operator can re-add hand-written comments if needed.
The high-value scalar fields (`chat_ids`, `blocked_chat_ids`) are
emitted as flow-style inline lists so `git diff` shows a one-line
change.

If PyYAML is available, we use `yaml.safe_dump(default_flow_style=False)`
with a custom representer for the chat-id lists (flow style). If
PyYAML is missing (the simple-yaml fallback in `config.py`), we use a
hand-rolled serializer that emits the same canonical layout.

### Hot reload

Naive option: re-call `load_config(instance_dir)` + `env_values(...)`
on every poll iteration. The poll runs at most once a second under
normal load, so the cost is negligible.

Cached option: stat-based — track `(.env mtime, gateway.yaml mtime)`
in a tuple. Reload only when changed. `env_values` already does this;
add the same to a new `load_config_cached(instance_dir)`.

We pick the **cached** option since:
- The gateway runs many channels off the same config; reloading on
  every iteration multiplies the cost.
- Mtime watching is reliable on local FS for atomic-replace writes
  (`os.replace` updates mtime).

The Telegram channel holds a `_authority_cache` of resolved
`(allowed_set, blocked_set)`. After our own approve/deny write, we
explicitly bump the mtime-key + invalidate so we don't have to wait
for the OS clock to tick.

### SQLite chats table

The `auth_status` column stays for now, but:
- `_is_authorized` no longer reads it.
- `_record_chat` no longer writes it (auth-status default unchanged in
  source: `'pending'`).
- `_handle_my_chat_member` and `_handle_bot_added` no longer set it.
- `set_auth_status` is *not* removed — `jc-chats approve/deny` still
  uses it as an informational flag, but emits a warning that the
  authoritative store is now config files.

A follow-up PR can remove the column once we're confident no one
reads it. (One `jc-chats list` rendering touches it; we keep that as
a frozen historical view.)

### CLI: `jc-chats approve` / `jc-chats deny`

Updated to write config files instead of DB. Same UX:

```
$ jc chats approve 12345
chat 12345 approved (added to ops/gateway.yaml + .env)
```

The DB row, if present, gets its `auth_status` mirrored for back-
compat (so `jc-chats list` still shows green dots), but the config
file is the source of truth.

Migration helper — `jc chats migrate-to-config`:

```
$ jc chats migrate-to-config --dry-run
would add to ops/gateway.yaml chat_ids: [8130606104, -5074463934]
would add to ops/gateway.yaml blocked_chat_ids: [-5222293802, -1003979778138]

$ jc chats migrate-to-config
ops/gateway.yaml updated
.env updated (TELEGRAM_CHAT_IDS appended)
```

This walks `chats` rows where `auth_status in ('allowed', 'denied')`
and lifts them into config. One-shot. Idempotent.

Out of scope for this PR (filed as follow-up): full removal of
`auth_status` column once migrated instances confirm green.

## Migration plan

Per-instance, one-shot:

1. Operator pulls the framework update.
2. On gateway start, an info log line is emitted on every instance
   that has DB-approved rows not present in `chat_ids`:
   `"telegram: N DB approvals not in config — run 'jc chats migrate-to-config'"`.
3. Operator runs `jc chats migrate-to-config`.
4. Re-run gateway. The auth check now reads from yaml + env.
5. No DB prune step in this phase. `auth_status` remains a `TEXT NOT NULL`
   historical/status column until a later schema migration removes or replaces
   it. Operators should not be told to null it out, and the CLI must not expose
   a prune flag unless the schema is changed in the same release.

For `rachel_zane` specifically: operator's primary DM `28547271` is
already in `chat_ids`. Secondary `8130606104` is not — needs to be
either approved through the new flow or migrated. The two pre-allowed
groups are already in `chat_ids`. Two `denied` rows
(`-5222293802`, `-1003979778138`, both Luca's test groups) need to
go to `blocked_chat_ids`.

## Test plan

New tests under `tests/gateway/test_sender_approval_config.py`:

1. `test_authorize_reads_yaml_chat_ids` — chat_id in yaml `chat_ids`
   → authorized; no SQLite touched (assert via patched `chats_module`).
2. `test_authorize_reads_env_chat_ids` — chat_id in `TELEGRAM_CHAT_IDS`
   → authorized.
3. `test_blocklist_short_circuits` — chat_id in both `chat_ids` and
   `blocked_chat_ids` → not authorized (blocklist wins).
4. `test_unknown_chat_default_deny` — chat_id in neither list, not
   the operator DM → not authorized.
5. `test_approve_writes_yaml_and_env_atomically` — simulate callback
   with `chat_auth:allow:99999` → after the call:
   - yaml `chat_ids` includes `99999`,
   - `.env` `TELEGRAM_CHAT_IDS` includes `99999`,
   - both files written via `.tmp` + replace (assert via stat
     mtime/inode bump).
6. `test_reject_writes_blocklist_only` — `chat_auth:deny:88888` →
   yaml `blocked_chat_ids` includes `88888`, `.env` untouched.
7. `test_approve_idempotent` — re-tap allow on already-approved
   chat → no file write (mtime stable), no error.
8. `test_blocked_then_approved_promotion` — chat in `blocked_chat_ids`,
   operator taps allow → chat moved from blocked → allowed.
9. `test_reload_picks_up_external_yaml_edit` — start gateway, write
   to yaml externally, next auth check reads new value (no restart).
10. `test_atomic_write_crash_safety` — patch `os.replace` to raise
    after `tmp.write_text` → assert original file unchanged.

Existing tests to update:
- `test_telegram_sender_approval.py` — drop the DB-status-based
  assertions, replace with config-based.
- `test_telegram_chat_recording.py:test_legacy_allowlist_overrides_pending`
  → renamed to `test_yaml_allowlist_authorizes` (DB has nothing to
  override anymore).

Plus full `pytest tests/` to confirm no other consumer relies on
`_is_authorized` reading from DB.

## File-by-file changes

- `lib/gateway/config.py` — add `blocked_chat_ids` field on
  `ChannelConfig`, parse list, validate; add `load_config_cached`.
- `lib/gateway/config_writer.py` (new) — `atomic_write_text`,
  `update_yaml_chat_lists`, `update_env_chat_ids`.
- `lib/gateway/channels/telegram.py` — rewrite `_is_authorized` to
  consult cached cfg only; rewrite `_handle_callback_query` to write
  config files; keep the early-poll `blocked_chat_ids` short-circuit before
  `_record_chat` so denied traffic does not create fresh audit rows; remove
  `_get_chats_conn` reads from the authorization path.
- `lib/gateway/channels/telegram_chats.py` — stop passing
  `auth_status` from `record_chat` (already does).
- `bin/jc-chats` — add `migrate-to-config` subcommand; rewrite
  `approve`/`deny` to call the new config writers.
- `tests/gateway/test_sender_approval_config.py` (new) — see test
  plan above.
- Delete (or rewrite) `tests/gateway/test_telegram_sender_approval.py`
  to reflect new architecture.

## Done criteria

- All sqlite reads/writes for auth removed from the auth path
  (grep `lib/gateway/channels/telegram.py` for `chats_module` shows
  zero hits inside `_is_authorized` and the early-poll check).
- Approval/rejection flows write config files atomically.
- Hot-reload works without gateway restart (new tests pass).
- All tests green: `pytest tests/`.
- PR opened with body referencing this spec + bugs #1–#4.
