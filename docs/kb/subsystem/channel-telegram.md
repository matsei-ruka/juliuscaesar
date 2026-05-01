---
title: Telegram gateway channel
section: subsystem
status: active
code_anchors:
  - path: lib/gateway/channels/telegram.py
    symbol: "class TelegramChannel:"
  - path: lib/gateway/channels/telegram.py
    symbol: "def _is_authorized"
  - path: lib/gateway/channels/telegram.py
    symbol: "def _handle_callback_query"
  - path: lib/gateway/channels/telegram.py
    symbol: "def _authority_sets"
  - path: lib/gateway/channels/telegram.py
    symbol: "def _maybe_send_sender_approval_prompt"
  - path: lib/gateway/channels/telegram_commands.py
    symbol: "def parse_slash_command"
  - path: lib/gateway/chats.py
    symbol: "def upsert_chat"
last_verified: 2026-05-01
verified_by: l.mattei
related:
  - subsystem/gateway-queue.md
---

## Summary

Long-poll `getUpdates` channel. Inbound text, voice/audio/video_note (transcribed via DashScope), photos, and documents enqueue events. Outbound replies and typing indicators go through `telegram_outbound`. Group/supergroup chats run through an Allow/Deny inline-keyboard auth gate before any message reaches the brain.

## Subscribed update types

`getUpdates` opts in via `_ALLOWED_UPDATE_TYPES`:

- `message`, `edited_message` — user content.
- `my_chat_member` — bot added/kicked. Drives `pending` row + auth prompt.
- `callback_query` — operator's tap on the Allow/Deny inline keyboard.

## Auth gate (config-only, default-deny)

**Authority lives in config files, not SQLite.** Re-architected after PR #28 / commit 93d4060 (security: default-deny). `chats` table is now observability only; `auth_status` field is unused on the dispatch path.

`_authority_sets()` returns `(allowed, blocked)` from:
- `ops/gateway.yaml` `channels.telegram.chat_ids` — allowed.
- `ops/gateway.yaml` `channels.telegram.blocked_chat_ids` — blocked.
- `.env` `TELEGRAM_CHAT_IDS` — allowed (legacy).

`_is_authorized(chat_id)` precedence (default-deny):

1. `chat_id ∈ blocked` → reject. Blocklist always wins over allowlist.
2. `chat_id ∈ allowed` → allow.
3. `chat_id == TELEGRAM_CHAT_ID` (operator's main DM) → allow.
4. Otherwise → drop silently.

### Approval flows

Two separate prompt paths, both ending in a `chat_auth:{allow|deny}:{chat_id}` inline-keyboard tap:

- **`my_chat_member`** (bot newly added to a group): `_handle_bot_added` DMs the operator. On `left|kicked`, `_block_chat` adds the chat to `blocked_chat_ids` so a re-add does not silently re-prompt.
- **Unauthorized inbound message**: `_maybe_send_sender_approval_prompt` (PR #28) DMs the operator with a 100-char preview when an unknown sender messages. De-duped via in-process `_auth_prompts_sent` set; the message itself is NOT enqueued or transcripted until approval.

`_handle_callback_query` validates `from.id == TELEGRAM_CHAT_ID`, then `_approve_chat` / `_block_chat` mutate `ops/gateway.yaml` (and `.env` `TELEGRAM_CHAT_IDS`) atomically. `_is_authorized` re-reads config every poll so the flip takes effect with no daemon restart.

### Config-only mode (PR #30, a32c5cc)

`sender_approval.mode` in `gateway.yaml`:

- `gateway` (default): full interactive prompts as above.
- `config_only`: no prompts; only chats already on the allowlist work. For headless / test setups.
- `off`: legacy permissive (not recommended; default-deny is the security posture).

## Cached SQLite connection

`_chats_conn` (observability table for `bin/jc-chats`) is opened lazily by `_get_chats_conn()` and reused inside the poll loop. **The poller calls `self.close()` at the end of every batch.** Even though authority is now config-only, closing per cycle keeps connection counts bounded and avoids stale snapshots when the `chats` table is updated by a sibling process (e.g. `bin/jc-chats`).

## Conversation id

`conversation_id = f"{chat_id}:{thread_id}"` when `message_thread_id` is set, else just `chat_id`. Threaded forum chats resume the same brain session per topic.

## Media

- voice / audio / video_note → `_ingest_audio_attachment` → `_transcribe_audio` (DashScope). On success, `meta.was_voice = True` so downstream renders a voice reply.
- photo → `_ingest_photo`. Empty-text + photo gets `[image]` placeholder.
- document → `_ingest_document`. Empty-text + document gets `[document: {file_name}]` placeholder.

## Slash commands

`telegram_commands.parse_slash_command` strips a leading `/cmd[@bot]` and dispatches via `handle_slash_command`. Built-in commands (commit 2f9fda0): `/help`, `/models`, `/compact`. Unknown commands fall through to normal text dispatch.

## Module split

`telegram.py` (the `TelegramChannel` orchestrator) is now thin. Helpers live in sibling modules:

- `telegram_chats.py` — `chats` table interactions for the directory.
- `telegram_commands.py` — slash command parser + handlers.
- `telegram_media.py` — voice/photo/document ingest + DashScope transcription.
- `telegram_outbound.py` — send / typing / message edit, MarkdownV2 escaping via `lib/gateway/format/escaper.py`.
- `telegram_routing.py` — conversation_id derivation, threaded forum handling, mention parsing.

## Invariants

- Bot token reads from `<instance>/.env` via `cfg.token_env`; never logged.
- Default-deny: an unknown chat is silently dropped. Authority is config files only.
- Blocklist beats allowlist. A re-added group must be re-approved.
- The poll loop swallows exceptions as `info`-level logs and sleeps 5s. Hard errors do not crash the daemon; they need explicit log review.

## Open questions / known stale

- 2026-04-27: A typo (`self.close_conns()` instead of `self.close()`) shipped in commit 9c8a45a and went undiagnosed for hours because the poll loop's blanket `except` logged the AttributeError at `info` level. Worth bumping that branch to `warning`/`error` so log filters surface it.
- 2026-05-01: SQLite `chats.auth_status` field exists but is not consulted by `_is_authorized` anymore. Either drop the column or repurpose for observability — current state is mildly confusing for new readers.
