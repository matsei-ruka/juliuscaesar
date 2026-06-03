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

New precedence (target — one release after the deprecation
window described in "Migration notes"):

```
--chat-id  →  $TELEGRAM_CHAT_ID_OVERRIDE  →  $ORIGIN_CHAT_ID
           →  (error — no silent fallback)
```

This release ships the deprecation step (see "Migration notes"
below): the old `$TELEGRAM_CHAT_ID` / `.env TELEGRAM_CHAT_ID`
fallback still resolves, but emits a one-shot stderr warning so
every offending caller surfaces in logs before the removal.

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

## Migration notes

Dropping `TELEGRAM_CHAT_ID` (env var **and** `.env` value) from
`send_telegram.py`'s precedence is a **breaking change** for any caller
that today relies on the silent fallback to deliver — exactly the class
of caller this spec exists to flush out, but a flag-day removal would
turn currently-misrouting heartbeats into hard failures with no warning
window for operators.

**Chosen path: deprecation warning for one release.**

- This release (`origin-chat-id` impl): `send_telegram.py` still
  consults `$TELEGRAM_CHAT_ID` env var → `TELEGRAM_CHAT_ID` in
  instance `.env` after `ORIGIN_CHAT_ID`, **but** when the resolved
  chat_id came from either of those sources (i.e. nothing higher in
  the ladder matched), emit a single-line warning on stderr **before**
  the send:

  ```
  send_telegram: DEPRECATED — resolved chat_id from $TELEGRAM_CHAT_ID
  (env / .env). This fallback will be removed in the next release.
  Set ORIGIN_CHAT_ID in the caller, pass --chat-id, or export
  TELEGRAM_CHAT_ID_OVERRIDE.
  ```

  The warning fires once per process. Successful sends are
  unchanged; operators get a forensic trail in `heartbeat.log` /
  worker logs to find every offending caller before the removal.

- **Next release:** the deprecated branches are deleted; the
  resolution ladder collapses to the four levels the spec defines
  (`--chat-id` → `$TELEGRAM_CHAT_ID_OVERRIDE` → `$ORIGIN_CHAT_ID` →
  error). At that point the verbose `_format_no_chat_id_error` block
  becomes the only failure mode for a misconfigured caller.

Rationale for *not* gating behind `--allow-env-default`: an opt-in
flag preserves the silent-mis-routing footgun by making the unsafe
behavior the easier path (operators copy the wrapper that adds the
flag once, then never revisit). A noisy deprecation forces every
offending caller to surface itself in logs within one release cycle.

## Follow-up: stale L1 rules

Once this lands, the L1 rulesets on a handful of instances carry
guidance that was written specifically to patch around the missing
routing default. Those rules become **contradictory** the moment
`ORIGIN_CHAT_ID` ships, and leaving them in memory will keep the
brain second-guessing a routing layer that now Just Works.

**Concrete rule classes to expect (search patterns):**

- Any rule containing `push_message_sent=true` written as a routing
  workaround (e.g. "never use `push_message_sent=true` for
  inbound-event replies because Bash-direct sends fall through to
  the wrong chat"). After this lands, the Bash-direct path inherits
  `ORIGIN_CHAT_ID` and routes correctly — the rule's *reason*
  evaporates, but the rule itself will keep nudging the brain to
  avoid the canonical sender path.
- Any rule mentioning `TELEGRAM_CHAT_ID` as a fallback hazard
  (e.g. "wrap every `send_telegram.sh` call with
  `TELEGRAM_CHAT_ID_OVERRIDE=$EVENT_CHAT_ID` because otherwise it
  goes to Luca's DM"). After this lands, the wrapper is redundant —
  but the rule will keep adding boilerplate the runner provides for
  free.
- Any rule warning against "ad-hoc Bash sends from inside a brain
  session" on routing-safety grounds. After this lands the safety
  concern is gone (the gateway adapter exports `ORIGIN_CHAT_ID`),
  though prompt-layer reasons (canonicality, push-marker dedupe)
  may still apply — re-read each before deletion, don't blanket-rm.

**Fleet sweep checklist (post-merge — DO NOT touch fleet memory in
this PR):**

For each fleet instance below, grep `memory/L1/RULES.md` (and any
`memory/L2/learnings/*chat*`, `*routing*`, `*telegram*`) for the
patterns `push_message_sent`, `TELEGRAM_CHAT_ID`,
`TELEGRAM_CHAT_ID_OVERRIDE`, `EVENT_CHAT_ID`. For each hit:

1. Confirm the rule's *reason* is routing-fallback, not something
   else (e.g. a `push_message_sent=true` rule may exist for
   push-marker dedupe — that reason survives).
2. If routing-fallback is the only reason: delete the rule, log the
   deletion in the instance's HOT.md or commit message.
3. If routing-fallback is one of several reasons: rewrite the rule
   to drop the routing clause, keep the rest.

Instances to sweep (all instances running heartbeat tasks or
inbound-reply flows — i.e. effectively all of them; ordered by
likelihood-of-hits based on incident history):

- [ ] `sergio_dev_ops` (192.168.16.218:/opt/sergio_dev_ops) —
      flagged by the spec as carrying *two* such rules
- [ ] `rachel_zane` (192.168.3.246:/home/lucamattei/rachel_zane) —
      2026-05-21 incident origin; likely has a `learnings/`
      entry pointing at this trap
- [ ] `marco_de_luca`, `harold_finch`, `anika_rao`
      (192.168.3.246, shared `juliuscaesar/`)
- [ ] `mario_leone_coo`, `alex_morgan` (192.168.16.218, shared
      `juliuscaesar/`)
- [ ] `florian_dev_ops` (192.168.14.113), `sofia_almeida`
      (192.168.3.13)
- [ ] `adrian_wong` (.14.103), `sarah_liu` (.14.111),
      `victoria_hale` (192.168.3.13), `mikaela_cruz` (.14.108),
      `marta_bellini` (.14.107), `alicia_koh` (.14.109),
      `noah_bitwell` (.14.112), `christina_gkoutziani` (.14.116),
      `amara_okonkwo` (.14.117), `rafael_costa` (192.168.3.209),
      `daniel_mercer` (.14.104), `sophie_reinhardt` (.14.105),
      `elliot_alderson` (.14.114), `ethan_zhang` (.14.115),
      `francesco_datini` (.14.118), `chloe_o_brian` (.3.144)

Owners of each instance run the sweep on their own memory; this PR
does **not** modify any fleet instance's `memory/` directory.
