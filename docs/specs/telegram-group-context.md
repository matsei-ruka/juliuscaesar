# Telegram group session reuse + reply-to-bot + 1:1 group auto-reply

**Status:** Spec
**Author:** Rachel
**Date:** 2026-04-26

## Problem

The just-shipped group-mention filter (`feat/telegram-group-mentions`,
`telegram.py:_should_process_message`) made the bot stop spamming groups, but
left three usability gaps that surfaced as soon as it hit a real chat:

1. **No per-group session.** Brain conversation continuity (`claude --resume
   <uuid>`) is keyed on `(channel, conversation_id, brain)`. Group messages
   set `conversation_id = chat_id` (or `chat_id:thread_id`), but nothing
   verifies sessions actually persist per-group across messages — and the
   existing tests never exercise group flows. We need an explicit assertion
   plus a log line so any future regression is visible in the gateway log.

2. **No reply-to-bot detection.** If Luca replies to one of Rachel's
   messages in a group without typing `@rachel_zane_2_bot`, the message is
   dropped. That breaks normal conversational flow — replies are how humans
   continue a thread. Reply context is unambiguous: the user is talking to
   the bot.

3. **No 1:1 group auto-reply.** If Rachel and one human are the only members
   of a group, every message is implicitly addressed to her. Forcing the
   user to @-mention themselves into a 2-person room is hostile UX.

Bug observed in production (gateway log, events 73 + 74, 2026-04-26
04:21–04:24 UTC): a `route` log line is emitted, then **no `adapter start`
line ever appears** before the daemon is killed by the watchdog ~30 s
later. The lease-expired requeue picks the event up after 5 min on the new
daemon and processes it normally. Strong hypothesis: the binary log file
opened in `Brain.invoke` is buffered (8 KB block buffer in `ab` mode), the
"adapter start" header lands in the user-space buffer, `subprocess.Popen`
returns, `proc.communicate(...)` blocks for the brain call, the daemon
catches SIGTERM mid-call, and the buffer dies with the process. We need
diagnostic logging (and a flush) to confirm the hypothesis before patching
the underlying behavior.

## Goals

- Verify per-group brain session continuity with one log line + one test —
  no code change unless the test fails.
- Reply detection: `message.reply_to_message.from.id == bot_user_id`
  triggers processing in groups, even without an `@`-mention.
- Auto-reply in groups whose member count `<= 2`. Cached per-chat for 5 min
  to amortize the `getChatMemberCount` HTTP call.
- Lifecycle logging in `Brain.invoke` so the next "adapter never started"
  event leaves a forensic trail. Flush the binary log header so it can't be
  lost on a SIGTERM mid-call.
- All three features pluggable into the existing `_should_process_message`
  helper — no flow restructuring.

## Non-goals

- No fix for the dispatch hang itself. Diagnostic only — confirm root cause
  first, then fix in a follow-up if the logs prove the buffering theory (or
  point elsewhere).
- No new config knobs. 1:1 detection is automatic; reply-to-bot is
  unconditional in groups; session reuse is a verification, not a feature.
- No proactive `getChatMemberCount` poll — only resolved on first incoming
  group message, and only when the @-mention check would otherwise fail.

## Adapter dispatch diagnostic

Inside `lib/gateway/brains/base.py:Brain.invoke`:

1. Add `dispatch begin event=<id> brain=<name> model=<model>` log line via a
   logger callback passed in by the caller (or fall back to writing through
   the binary log if the caller doesn't pass one — for now we emit through
   the structured runtime logger by surfacing the call from `runtime.py`).
2. After the existing `adapter start` write to the binary log, **flush +
   fsync** the underlying file descriptor so the header survives a SIGTERM.
3. Add `adapter spawn event=<id> pid=<pid>` immediately after `Popen`
   returns. This is the critical signal: if this line is missing on a
   future hang, fork/exec itself is blocking; if it appears but the brain
   never returns, the brain subprocess is the culprit.
4. Add `adapter exit event=<id> pid=<pid> rc=<code> duration=<sec>` after
   `communicate` returns (or the timeout/kill path).
5. On exception: `adapter failed event=<id> reason=<short>`.
6. In `runtime.py`, emit `dispatch begin id=<id> brain=<brain> model=<m>
   resume=<sid|none>` immediately before `invoke_brain`, and log
   `requeued events ids=[...]` on every requeue (currently only the count
   is logged).

The `adapter start` line stays in the binary log (don't move it) so the
existing grep tooling keeps working — we just teach it to flush before the
Popen.

## Feature 1: per-group session reuse (verify)

`telegram.py:240` already builds `conversation_id = f"{chat_id}:{thread_id}"
if thread_id else chat_id`. `runtime.py:_resume_id` looks up
`(channel, conversation_id, brain)` against the `sessions` table. The
plumbing is correct.

Add:
- `session resume id=<event_id> conv=<conversation_id> brain=<brain>
  session=<uuid|none>` log emitted right after `_resume_id` returns, in
  `runtime.process_event`.
- Test `test_group_uses_per_chat_session_id` in `tests/gateway/test_channels.py`:
  drive the channel with two updates from the same group `chat_id` and one
  from a different group; assert the `conversation_id` in the captured
  enqueue kwargs matches the chat_id (no thread) and is the same for the
  two same-group updates, different for the third.

No behavior change. If the test passes, sessions are already per-group.

## Feature 2: reply-to-bot detection

In `_should_process_message`, inside the `group/supergroup` branch, **before**
the entity scan:

```python
reply_to = message.get("reply_to_message") or {}
reply_from = reply_to.get("from") or {}
if self.bot_user_id and reply_from.get("id") == self.bot_user_id:
    return True
```

Why before the entity scan: if both signals are present (user @-mentions
the bot in a reply to its own message), short-circuit on the cheaper check.
Why guard on `bot_user_id`: same fail-closed semantics as the existing
`bot_username` guard — if `getMe` hasn't resolved yet, we can't verify, so
we skip.

Tests:

- `test_group_reply_to_bot_processed` — `chat.type=group`, no entities, but
  `reply_to_message.from.id == bot_user_id` → `True`.
- `test_group_reply_to_human_not_processed` — `reply_to_message.from.id`
  is a different user id, no mention → `False`.

## Feature 3: auto-reply in 2-person groups

State on `TelegramChannel`:

```python
self._member_count_cache: dict[str, tuple[int, float]] = {}
```

Method:

```python
def _get_chat_member_count(self, chat_id: str) -> int | None:
    cached = self._member_count_cache.get(chat_id)
    now = time.monotonic()
    if cached is not None and cached[1] > now:
        return cached[0]
    try:
        data = http_json(
            f"https://api.telegram.org/bot{self.token}/getChatMemberCount?"
            + urllib.parse.urlencode({"chat_id": chat_id}),
            timeout=10,
        )
    except Exception as exc:
        self.log(f"telegram getChatMemberCount failed chat_id={chat_id}: {exc}")
        return None
    if not data.get("ok"):
        return None
    count = data.get("result")
    if not isinstance(count, int):
        return None
    self._member_count_cache[chat_id] = (count, now + 300.0)
    return count
```

In `_should_process_message`, **before** the entity scan, after the
`bot_username` guard:

```python
chat_id = str(chat.get("id", ""))
member_count = self._get_chat_member_count(chat_id)
if member_count is not None and member_count <= 2:
    self.log(
        f"telegram group is 1:1 with bot count={member_count} chat_id={chat_id} — process all"
    )
    return True
```

The 1:1 log line fires on every match (cheap, useful for audit). TTL is 5 min:
group membership changes rarely, and the worst case is a 5-minute window
where a third joiner is treated as 1:1 (acceptable — they'd just see the
bot reply to one of their messages).

Tests:
- `test_group_with_two_members_replies_always` — patch
  `_get_chat_member_count` to return `2`, no mention → `True`.
- `test_group_with_three_members_requires_mention` — patch to return `3`,
  no mention → `False` (existing behavior preserved).

## Order of checks in `_should_process_message`

Final order in the group/supergroup branch:

1. Reply-to-bot (cheap, exact, no HTTP) → if hit, return True.
2. 1:1 detection (cached HTTP, fail-soft) → if hit, return True.
3. Existing entity scan + substring fallback for @-mention.
4. Otherwise return False.

## Backward compatibility

- DM behavior unchanged.
- Channel post behavior unchanged.
- Existing @-mention behavior unchanged.
- New behaviors are additive; the only way they affect a group that worked
  before is to make Rachel reply to a message she would have ignored.

## Failure modes

| Failure | Behavior |
|---|---|
| `getChatMemberCount` HTTP error | Cache miss returned; fall through to entity scan. |
| `getChatMemberCount` returns non-int | Cache miss; fall through. |
| Member count cache stale across a join/leave | Worst case 5 min of incorrect "1:1" status. |
| `reply_to_message.from` missing user id | `reply_from.get("id")` is `None`, comparison is `False`, no false positive. |
| `bot_user_id` not yet resolved | Reply-to-bot check is `False`; falls through. |

## Decisions to defer

- Pinning member-count refresh to `chat_member` updates would be more
  efficient than TTL polling, but requires switching to webhook mode or
  subscribing to `chat_member` updates explicitly. Out of scope.
- Reply-to-bot in DMs is meaningless (DMs are 1:1 already). Skip.
