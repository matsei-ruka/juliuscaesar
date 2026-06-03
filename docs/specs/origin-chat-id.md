# ORIGIN_CHAT_ID — session-scoped chat routing default

## Problem

`send_telegram.py` has four sources of truth for the destination chat,
in this precedence:

1. `--chat-id` CLI arg
2. `$TELEGRAM_CHAT_ID_OVERRIDE` env var
3. `$TELEGRAM_CHAT_ID` env var
4. `TELEGRAM_CHAT_ID` in instance `.env`

The bottom two are the trap. `TELEGRAM_CHAT_ID` in `.env` is the
*instance default* — almost always the operator's personal DM, because
that is the only chat known at install time. Any caller (heartbeat
task, ad-hoc Bash inside a brain session, watchdog ping) that forgets
to pass `--chat-id` or set `TELEGRAM_CHAT_ID_OVERRIDE` silently falls
through to that default. The user-visible failure is "the message
landed in the wrong chat" — almost always Luca's DM instead of the
group / customer thread that triggered the work.

This is not a hypothetical. Confirmed incidents:

- **2026-05-21 02:41 UTC** — reply to an ops-group message
  (`-5204064447`) was routed with `push_message_sent=true` (Bash-direct
  send). `TELEGRAM_CHAT_ID_OVERRIDE` was unset; the script fell through
  to the `.env` default (Luca's DM, 28547271) and the group reply
  landed in Luca's private chat. Recovery required a manual rule
  added to instance L1 memory ("never use push_message_sent=true for
  inbound-event replies") — i.e. patched at the brain-prompt layer
  rather than at the routing layer.
- Recurring class — every new heartbeat task added without
  `TELEGRAM_CHAT_ID_OVERRIDE` in its wrapper has shipped messages to
  the wrong chat at least once before the operator noticed. The L1
  ruleset for the sergio_dev_ops instance has *two separate rules*
  written specifically to remember this routing trap, which is itself
  evidence that the routing default is wrong.

The framework keeps re-paying for the same bug because the fallback is
silent: there is no error to act on, just a misdirected message that
the recipient may or may not catch.

## Solution

Two changes that together turn silent mis-routing into either a correct
delivery or a loud error.

### 1. Gateway + HB runner export `ORIGIN_CHAT_ID`

Every code path that *originates* an interaction already knows the
"home" chat for that interaction:

- **Gateway brain adapter** (`lib/gateway/brains/base.py`): the event
  being dispatched has `event.metadata["chat_id"]` (for telegram /
  whatsapp / slack channels) — that is the chat the reply should
  normally go back to.
- **Heartbeat runner** (`lib/heartbeat/runner.py`): a task has at
  most one destination (single or multi-fanout via `destinations:`
  block in `tasks.yaml`). For the single-destination case the
  resolved `chat_id` is the home chat for any sender invoked inside
  the task.

Both layers shall export `ORIGIN_CHAT_ID` into the subprocess env they
build, so any `send_telegram.py` call inside the brain session or task
script inherits it without further plumbing.

Multi-destination tasks (the rare fanout case) shall *not* export
`ORIGIN_CHAT_ID` — there is no single origin, and the existing
per-send loop in the runner already provides `TELEGRAM_CHAT_ID_OVERRIDE`
per destination. The new variable is a default, not a replacement.

### 2. `send_telegram.py` precedence change

Old precedence:

```
--chat-id  →  $TELEGRAM_CHAT_ID_OVERRIDE  →  $TELEGRAM_CHAT_ID
           →  TELEGRAM_CHAT_ID in instance .env   (silent default)
```

New precedence:

```
--chat-id  →  $TELEGRAM_CHAT_ID_OVERRIDE  →  $ORIGIN_CHAT_ID
           →  (error — no silent fallback)
```

The `.env` `TELEGRAM_CHAT_ID` value is retained *as installation
metadata only* (the gateway still reads it for owner-DM lookups, etc.)
but `send_telegram.py` no longer consults it. The `$TELEGRAM_CHAT_ID`
env var is also dropped from the precedence list to remove the second
silent default (which currently shadows `ORIGIN_CHAT_ID` if anything
upstream exports it).

The error on missing chat_id is verbose and actionable:

```
[send_telegram] ERROR: no chat_id resolved.
  Checked: --chat-id (none), TELEGRAM_CHAT_ID_OVERRIDE (unset),
           ORIGIN_CHAT_ID (unset)
  Known chats (from <instance>/memory/L1/CHATS.md):
    28547271       Luca Mattei (private)
    -5204064447    Scovai Dev Coordination (group)
    -5203889506    Scovai Support Coordination (group)
    223588914      Phil / Brakio (private)
  Fix one of:
    - Inbound event reply: ensure the gateway brain adapter exported
      ORIGIN_CHAT_ID (bug in lib/gateway/brains/base.py if absent).
    - HB / cron task: add `destination:` to the task in tasks.yaml,
      or wrap the call with `TELEGRAM_CHAT_ID_OVERRIDE=<id>` for
      one-offs.
    - Manual one-off: pass --chat-id <id> explicitly.
```

The known-chats list is read from `<instance>/memory/L1/CHATS.md` if
present (the auto-generated index already exists for every instance);
fall back to omitting that block if the file isn't there.

### Roles of each variable, after the change

| Variable                     | Set by                              | Used by                  | Meaning                                            |
|------------------------------|-------------------------------------|--------------------------|----------------------------------------------------|
| `--chat-id` (CLI)            | Caller, explicit                    | send_telegram.py         | "Send to exactly this chat regardless of context." |
| `TELEGRAM_CHAT_ID_OVERRIDE`  | Caller, explicit (env)              | send_telegram.py         | Same as --chat-id, but env-var form for Bash chains. |
| `ORIGIN_CHAT_ID`             | Gateway adapter, HB runner          | send_telegram.py         | "The chat this session is anchored to."            |
| `TELEGRAM_CHAT_ID` (`.env`)  | Operator at install                 | Gateway internals only   | Owner-DM identity / install metadata. No longer consulted by send_telegram.py. |

Explicit always beats implicit: `--chat-id` and `OVERRIDE` exist for
the legitimate cross-channel push case (a brain session anchored to
Luca's DM that needs to fire an alert to the ops group) and they
continue to win over the new default.

## Implementation pointers

These are the concrete edit points the implementation PR will touch.
This spec PR does **not** include the code changes — it only sets the
contract.

### `lib/gateway/brains/base.py`

Around line 355 where the env dict is built (`env = os.environ.copy()`
followed by `JC_INSTANCE_DIR`, `JC_EVENT_SOURCE`, etc.), add:

```python
chat_id = (event.metadata or {}).get("chat_id")
if chat_id:
    env["ORIGIN_CHAT_ID"] = str(chat_id)
else:
    env.pop("ORIGIN_CHAT_ID", None)
```

The `pop()` matters: the parent process may carry an `ORIGIN_CHAT_ID`
from a previous invocation, and inheriting it into an event with no
metadata chat_id would cause cross-event bleed. Explicit reset.

### `lib/heartbeat/runner.py`

Around line 270 where `TELEGRAM_CHAT_ID_OVERRIDE` is set for the
post-task send, add a parallel branch for the brain-invocation env:
when the task has exactly one resolved destination, export
`ORIGIN_CHAT_ID=<dest.chat_id>` into the env passed to the brain
subprocess. Multi-destination tasks must *not* set it (see "Solution"
above). The existing `TELEGRAM_CHAT_ID_OVERRIDE` logic on the
per-destination send loop is unchanged.

### `lib/heartbeat/lib/send_telegram.py`

`main()` precedence block (lines ~223-233) becomes:

```python
chat_id = (
    args.chat_id
    or os.environ.get("TELEGRAM_CHAT_ID_OVERRIDE")
    or os.environ.get("ORIGIN_CHAT_ID")
)
if not chat_id:
    sys.exit(_format_no_chat_id_error(instance, args))
```

The `_load_env_file` call drops `TELEGRAM_CHAT_ID` from the requested
keys (only `TELEGRAM_BOT_TOKEN` remains). `_format_no_chat_id_error`
emits the verbose block from the "Solution" section above, with the
known-chats list loaded from `<instance>/memory/L1/CHATS.md` when
present.

The module docstring's "Precedence for chat_id" section (lines 17-21)
is rewritten to match the new precedence.

## Acceptance

1. **Inbound telegram event → reply**: a Bash `send_telegram.py` call
   inside the brain session, with no flags and no `OVERRIDE`, delivers
   to the chat the event came from (because the gateway exported
   `ORIGIN_CHAT_ID`). The 2026-05-21 incident no longer recurs.
2. **HB task with single `destination:`**: a `send_telegram.py` call
   inside the task script, with no flags and no `OVERRIDE`, delivers
   to the destination chat. Tasks like `email-watch`, `log-watch`,
   `mike-watch` need no per-call boilerplate.
3. **HB task with multi-fanout `destination:` list**: `ORIGIN_CHAT_ID`
   is unset; the existing per-destination loop's
   `TELEGRAM_CHAT_ID_OVERRIDE` continues to drive routing.
4. **Manual one-off** (operator on the shell): `--chat-id` or
   `TELEGRAM_CHAT_ID_OVERRIDE` is required. Running
   `send_telegram.py` with neither exits non-zero with the verbose
   error block. `TELEGRAM_CHAT_ID` in `.env` is no longer a fallback.
5. **Cross-channel push from a session**: a brain anchored to chat A
   that wants to push to chat B passes `--chat-id B` or
   `TELEGRAM_CHAT_ID_OVERRIDE=B`; both still win over `ORIGIN_CHAT_ID`.
6. **Migration**: existing per-task wrappers that already set
   `TELEGRAM_CHAT_ID_OVERRIDE=<id>` continue to work unchanged
   (explicit always beats implicit). The cleanup PR that removes
   now-redundant `OVERRIDE` wrappers is *not* in scope here — this
   spec only enables the new behavior; cleanup is a separate, optional
   follow-up.
7. **Error visibility**: a misconfigured cron (task with no
   `destination:` and no in-script `OVERRIDE`) fails the send with a
   non-zero exit and the verbose error in stderr — surfaces in
   `heartbeat.log` rather than producing a misdirected message.

## Out of scope

- Rewriting the gateway's owner-DM lookups to use anything other than
  `.env`'s `TELEGRAM_CHAT_ID`. The variable retains its install-metadata
  role; only `send_telegram.py` stops consulting it.
- Whatsapp / Slack / Discord channel parity. Those channels have their
  own sender scripts and are not currently part of the
  `send_telegram.py` precedence trap. A parallel `ORIGIN_<CHANNEL>_ID`
  scheme can be added later if those channels develop the same class
  of bug; this spec covers Telegram only.
- Per-instance overrides to *re-enable* the `.env` fallback. If an
  operator wants a "default chat for any sender that forgot to
  specify", they can wrap the sender call in
  `TELEGRAM_CHAT_ID_OVERRIDE=<id>` at the systemd-unit / cron level.
  Adding a feature flag for the old behavior would preserve the
  silent-mis-routing footgun the spec exists to remove.
