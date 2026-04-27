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
  - path: lib/gateway/chats.py
    symbol: "def set_auth_status"
last_verified: 2026-04-27
verified_by: Rachel Zane
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

## Auth gate

`chats` table (`lib/gateway/chats.py`) tracks `auth_status ∈ {pending, allowed, denied}` per `(channel, chat_id)`.

Flow:

1. `my_chat_member` with `new_status ∈ {member, administrator}` → `_handle_bot_added` upserts the chat as `pending` (skipped if already `allowed`/`denied`) and DMs the operator with an inline keyboard whose `callback_data = chat_auth:{allow|deny}:{chat_id}`.
2. Operator taps → `_handle_callback_query` validates `from.id == TELEGRAM_CHAT_ID`, calls `set_auth_status(...)`, edits the prompt message, and on deny calls `leaveChat`.
3. Each subsequent message hits `_is_authorized(chat_id)`:
   - Env allowlist (`cfg.chat_ids`) → allow (legacy).
   - DM with operator (`chat_id == TELEGRAM_CHAT_ID`) → allow.
   - `chats.auth_status == 'allowed'` → allow.
   - Lookup error → fail open (logged).
   - Row missing → fail open (next ingest will record).
   - Else → drop silently.

## Cached SQLite connection

`_chats_conn` is opened lazily by `_get_chats_conn()` and reused inside the poll loop to skip per-call `init_db` churn. **The poller calls `self.close()` at the end of every batch.** The callback handler commits `auth_status='allowed'` on a separate write; under SQLite WAL isolation a long-lived reader holds a snapshot from before that commit and would keep seeing `pending`. Closing per cycle forces a fresh read on the next iteration so an Allow tap takes effect on the very next inbound message.

## Conversation id

`conversation_id = f"{chat_id}:{thread_id}"` when `message_thread_id` is set, else just `chat_id`. Threaded forum chats resume the same brain session per topic.

## Media

- voice / audio / video_note → `_ingest_audio_attachment` → `_transcribe_audio` (DashScope). On success, `meta.was_voice = True` so downstream renders a voice reply.
- photo → `_ingest_photo`. Empty-text + photo gets `[image]` placeholder.
- document → `_ingest_document`. Empty-text + document gets `[document: {file_name}]` placeholder.

## Slash commands

`telegram_commands.parse_slash_command` strips a leading `/cmd[@bot]` and dispatches via `handle_slash_command`. Unknown commands fall through to normal text dispatch.

## Invariants

- Bot token reads from `<instance>/.env` via `cfg.token_env`; never logged.
- Group chats never bypass the auth gate. The env allowlist (`TELEGRAM_CHAT_ID` etc.) is the only legacy escape hatch.
- `_chats_conn` is closed after every poll batch so WAL writes from `callback_query` are visible immediately. Method name is `close()` — there is no `close_conns()` (see 2026-04-27 incident in `## Open questions / known stale`).
- The poll loop swallows exceptions as `info`-level logs and sleeps 5s. Hard errors do not crash the daemon; they need explicit log review.

## Open questions / known stale

- 2026-04-27: A typo (`self.close_conns()` instead of `self.close()`) shipped in commit 9c8a45a and went undiagnosed for hours because the poll loop's blanket `except` logged the AttributeError at `info` level. Worth bumping that branch to `warning`/`error` so log filters surface it.
