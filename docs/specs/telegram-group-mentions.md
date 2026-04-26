# Telegram group-mention filter — only respond when @-mentioned in groups

**Status:** Spec
**Author:** Rachel
**Date:** 2026-04-26

## Problem

`TelegramChannel.run()` (`lib/gateway/channels/telegram.py:84`) enqueues every
message from any allowed chat. When the bot is added to a group or supergroup,
that means **every group message** (greetings, off-topic chatter, replies
between humans) becomes a triage event and the bot answers each one.

That blows out cost and makes the bot a nuisance — groups should only get a
reply when someone explicitly addresses the bot. DMs, by contrast, are
inherently 1:1 and should stay unchanged.

## Goals

- Group / supergroup messages only enqueue when the bot is **@-mentioned**
  (or referenced via `text_mention` to the bot's user_id).
- DMs (`chat.type=private`) keep current behavior — every message enqueues.
- Channel posts (`chat.type=channel`) keep current behavior — every message
  enqueues (channels are broadcast, the bot is the only sender semantics
  there are different).
- No new required config. The bot's username must be discovered, not
  configured: avoids drift on token rotation, avoids one more env var to
  forget.

## Non-goals

- No per-chat allow/deny knobs beyond the existing `chat_ids` allowlist.
- No "reply when someone replies to one of the bot's messages." Could come
  later; spec note only.
- No mention-detection for languages without `@` prefix syntax — Telegram
  doesn't have any, so moot.

## `chat.type` semantics

Telegram's `Chat` object has a `type` field. Possible values and our handling:

| `type`        | Source           | Behavior                  |
|---------------|------------------|---------------------------|
| `private`     | DM with a user   | Enqueue (current).        |
| `group`       | Basic group      | Enqueue **only if mention**. |
| `supergroup`  | Large group      | Enqueue **only if mention**. |
| `channel`     | Broadcast channel| Enqueue (current).        |
| (missing)     | Older API / bug  | Treat as `private`.       |
| (other)       | Future-proof     | **Fail closed** — drop.   |

## Bot username discovery

**Decision: call `getMe` once at channel startup, cache `username` and `id`
on `self`.**

- Why not config: forces the operator to set a new env var, easy to forget,
  drifts on token rotation. The bot already has the token; `getMe` is
  authoritative.
- Why not every message: rate-limit waste; username only changes if the bot
  is renamed, which requires a manual operator action. One call per process
  lifetime is plenty.
- Failure handling: if `getMe` fails (network blip at boot), `bot_username`
  stays `None`. Groups then **fail closed** (drop) — better silent than
  noisy. DMs still flow because they don't depend on it. Next poll iteration
  retries the `getMe` call.

Implementation: `_resolve_bot_username()` is called lazily on the first
`getUpdates` poll inside `run()` (not `__init__`) so a missing token at
construction doesn't crash the channel startup, and so retries on transient
failure are automatic.

## Mention detection strategy

**Decision: prefer Telegram's `entities` array; fall back to case-insensitive
substring match only when entities are missing.**

Telegram tags `@username` strings inside a message with `MessageEntity`
objects in `message.entities` (or `message.caption_entities` for
photo/document captions). Two relevant types:

- `mention` — the literal substring `@username` matches a Telegram username.
  Has `offset` and `length`. To get the username, slice the text and strip
  the `@`.
- `text_mention` — used when the user has no public username. Carries a
  `user` object with `id`. Compare against `self.bot_user_id`.

Why entities first:
- Handles edge cases automatically — `@botname` inside a code block is
  flagged as `code` by Telegram, NOT `mention`, so we won't trigger.
- Handles partial-word issues — `@botnameextra` is not a mention entity at
  all; substring match would false-positive.
- Handles unicode and non-ASCII names that regexes get wrong.

Fallback substring match is `f"@{bot_username}" in text.lower()` — only used
when `entities` is empty/missing. Caption text uses `caption_entities`. No
regex; lowercase compare is sufficient (Telegram usernames are ASCII and
case-insensitive).

## `_should_process_message()` helper

Signature:

```python
def _should_process_message(self, message: dict) -> bool: ...
```

Flow:

1. Read `chat.type`, default `"private"`.
2. `private` / `channel` → `True`.
3. Not `group` / `supergroup` → `False` (unknown type, fail closed).
4. `bot_username` is `None` → `False` (can't verify, fail closed).
5. Walk `entities` (or `caption_entities`):
   - `mention` entity: slice `text[offset:offset+length]`, strip `@`,
     lowercase, compare to `bot_username`.
   - `text_mention` entity: compare `entity.user.id` to `bot_user_id`.
6. If no entity matched, do the lowercase-substring fallback on `text`.
7. Return `False` otherwise.

## Hook point

Inside `run()`, after the existing allowed-chat check at
`lib/gateway/channels/telegram.py:103-105`, before `_log_forward`:

```python
if not self._should_process_message(message):
    self.log(f"telegram ignored non-mention chat_id={chat_id} type={chat.get('type')}")
    continue
```

`_resolve_bot_username()` is invoked at the top of the per-update loop,
guarded by `if self.bot_username is None and self.token:`.

## Backward compatibility

- DMs unchanged — `chat.type=private` always returns `True`.
- Existing `chat_ids` allowlist still runs first; if a group isn't in the
  allowlist, it's dropped before the mention check.
- No new required config field. `bot_username` is discovered. Operators who
  upgrade get the new behavior automatically.
- Channel posts unchanged. Voice/photo/document handling unchanged (helper
  is called before media ingestion, but media without text/caption in a
  group will be dropped — that's the desired behavior, a forwarded photo
  to a group shouldn't trigger the bot).

## Test plan

`tests/gateway/test_channels.py` — new `TelegramGroupMentionTests` class.
Mock `http_json` for any `getMe` call; construct `TelegramChannel`, set
`bot_username`/`bot_user_id` on the instance directly, call
`_should_process_message(message)` with hand-built dicts. Don't start the
poller (the helper is pure given state).

Cases:

- `test_dm_replied_always` — `chat.type=private`, no entities → `True`.
- `test_group_message_ignored_if_not_mentioned` — `chat.type=group`, plain
  text, no entities → `False`.
- `test_group_message_replied_if_mentioned` — `chat.type=group`, text
  `"hey @rachelbot hi"` with `mention` entity at offset 4 length 11 → `True`.
- `test_supergroup_mention_detection` — `chat.type=supergroup`, `text_mention`
  entity carrying `user.id == bot_user_id` → `True`.
- `test_group_no_username_resolved_fail_closed` — `bot_username=None` →
  `False` even with mention text.
- `test_group_mention_case_insensitive` — text `"hi @RachelBot"`,
  `bot_username="rachelbot"`, no entities → `True` via fallback.

## Failure modes

| Failure | Behavior |
|---|---|
| `getMe` fails at boot | `bot_username` stays `None`. DMs flow; groups fail closed. Next poll retries. |
| Mention entity malformed (missing offset/length) | `slice` returns `""`, comparison fails, walk continues. |
| Telegram sends `entities` for a non-text field | Walk finds no `mention`/`text_mention`; falls back to substring on `text` (which may be `""`). Returns `False`. |
| Bot is renamed mid-process | New mentions stop working until restart. Acceptable — rare event, watchdog restart cycle is short. |

## Logging

Drop ledger line on every drop:

```
telegram ignored non-mention chat_id=<id> type=<group|supergroup>
```

`getMe` resolution:

```
telegram bot_username resolved as @<name> (id=<id>)
telegram getMe failed: <err>   # only on failure
```

## Decisions to defer

- **Reply-to-bot detection.** If a user replies to one of the bot's
  messages without @-mentioning it, current spec drops the reply. Could
  add `message.reply_to_message.from.id == bot_user_id` as a third trigger
  later. Defer until requested.
- **Per-chat-type override config.** No real-world need yet.
- **Slash-command filter.** `/start`, `/help` etc. — not in scope; the
  brain can decide what to do with them once they pass mention filter.
