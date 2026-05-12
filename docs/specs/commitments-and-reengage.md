# Spec: Commitments & Re-engagement

**Status:** Draft — pending Luca review
**Date:** 2026-05-11
**Scope:** Two coordinated subsystems for autonomous agent behavior:
- `jc-commitments` — action engine that fires deferred YAML-defined actions on schedule
- `jc-reengage` — silence detector that produces re-engagement commitments
**Branch:** `feat/commitments-and-reengage`

---

## Goal

Today JC agents are reactive only: they respond when pinged, and the heartbeat cron fires fixed pre-configured tasks. There's no general mechanism for:

1. **Deferred follow-through.** "Ti scrivo giovedì" is a commitment the agent makes in conversation, then forgets. There's no durable place to write "fire this message at this time."
2. **Silence-aware re-engagement.** When a tracked user goes quiet for >48h, no one notices. The agent doesn't proactively touch back without manual prompting.

Mario Leone (Scovai COO instance) solves both with `ops/commitments-tick.py` + manual queueing — that needs to graduate to framework code so every instance gets it via `git pull && ./install.sh`.

This spec covers both subsystems together because re-engagement is the first non-trivial *producer* of commitments. Designing them in isolation risks coupling that's hard to undo later.

---

## Architecture overview

```
┌─────────────────────────────────────────────────────────────────────┐
│ Producers (write commitment YAML)                                   │
│  ├─ Agent (in conversation): writes when committing to future act   │
│  ├─ jc-reengage builtin: writes re-engagement touches               │
│  ├─ Heartbeat tasks: any builtin can queue                          │
│  └─ Operator: jc-commitments add (manual)                           │
└───────────────────────────────┬─────────────────────────────────────┘
                                ▼
              state/commitments/<slug>.yaml  (single source of truth)
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Engine (jc-commitments tick — heartbeat builtin, default */5 min)   │
│  ├─ Scan all pending YAMLs                                          │
│  ├─ Fire those with due_at <= now                                   │
│  ├─ Archive executed → state/commitments/done/                      │
│  └─ On failure: retry up to 3x, then move to failed/                │
└───────────────────────────────┬─────────────────────────────────────┘
                                ▼
                     Action dispatch (per action type)
                                ▼
                    telegram-send | jc-event | (later) email-send
```

Producers and engine are decoupled — only the YAML schema and the state dirs are shared.

---

## Subsystem 1 — `jc-commitments`

### Binary + module

- Binary: `bin/jc-commitments`
- Module: `lib/commitments/`
  - `lib/commitments/schema.py` — pydantic model + YAML I/O
  - `lib/commitments/engine.py` — tick loop, retry, archive
  - `lib/commitments/actions.py` — action dispatchers (telegram_send, jc_event)
- Heartbeat builtin: `lib/heartbeat/builtins/commitments_tick.py`

### Subcommands

```
jc-commitments tick                         # scan + fire due commitments (used by heartbeat)
jc-commitments add <slug> --due <iso>       # create a YAML programmatically
       --action telegram-send --chat-id <id> --text <body>
       [--tags <csv>] [--repeat daily|weekly] [--origin <label>]
jc-commitments list [--status pending|done|failed|all]
jc-commitments show <slug>                  # cat + parse + validate one
jc-commitments cancel <slug>                # delete a pending YAML
jc-commitments cancel --tag <name>          # delete all pending with tag (used by reengage reset)
```

### YAML schema

Path: `state/commitments/<slug>.yaml` (pending) → `state/commitments/done/<slug>.executed-<UTC>.yaml` (executed) → `state/commitments/failed/<slug>.failed-<UTC>.yaml` (after 3 retries).

```yaml
slug: deal-x-follow-up            # required; must match filename stem
created_at: "2026-05-11T14:30:00+04:00"  # ISO 8601 with TZ offset
due_at: "2026-05-15T09:00:00+04:00"      # ISO 8601 with TZ offset
action: telegram-send             # telegram-send | jc-event
chat_id: 28547271                 # required for telegram-send
text: |                           # supports MarkdownV2; gateway escaper applies
  Filippo, come promesso — update su X.
tags: [follow-up, deal-x]         # optional; used for filtering + cancel-by-tag
repeat: null                      # null | daily | weekly — null is one-shot
origin: agent                     # agent | reengage | heartbeat | manual
metadata:                         # action-specific, free-form
  retries: 0                      # auto-managed by engine on failure
```

**Validation rules:**
- `slug` matches `^[a-z0-9][a-z0-9-]{0,63}$`
- `due_at` must parse as ISO 8601 with explicit TZ
- `action` must be in the action-dispatch registry
- `text` required for `telegram-send`; max 4000 chars
- `chat_id` integer; if not provided, falls back to `$TELEGRAM_CHAT_ID` from `.env`

### Tick behavior

1. Read all files matching `state/commitments/*.yaml` (excluding subdirs)
2. For each: parse + validate. On parse error, log + skip (don't archive — operator inspects).
3. If `due_at > now`: skip (not yet due)
4. If `due_at <= now`: dispatch via action handler
5. On dispatch success:
   - If `repeat: null` → move file to `state/commitments/done/<slug>.executed-<UTC>.yaml`
   - If `repeat: daily/weekly` → rewrite same file with `due_at = due_at + interval`, append to `done/` an archived copy
6. On dispatch failure:
   - Increment `metadata.retries`
   - If `retries >= 3` → move to `state/commitments/failed/<slug>.failed-<UTC>.yaml`, log to `state/commitments-tick.log`
   - Else: leave in place, retry on next tick

### Action handlers

**`telegram-send`:**
- Re-use existing `lib/channels/telegram/sender.py` (the same module used by heartbeat tasks for output)
- MarkdownV2 escaping handled by `lib/gateway/format/escaper.py:to_markdown_v2`
- Failure modes: 4xx Telegram error → no retry (likely permanent: bad chat_id, blocked bot); 5xx or network → retry

**`jc-event`:**
- Emit a jc-event with payload `{slug, tags, action_metadata}` to `state/events/`
- Lets one instance trigger another via cross-instance event bus
- Useful for fleet-level coordination (out of scope for v1 but schema-ready)

**Future: `email-send`** — out of scope for v1.

### Cron / scheduling

Default heartbeat config (added to `templates/init-instance/heartbeat/tasks.yaml`):

```yaml
commitments_tick:
  builtin: commitments_tick
  enabled: false   # operator opts in
  # No cron schedule field — heartbeat runner reads default from builtin:
  # default_schedule: "*/5 * * * *"
```

Operator enables in their `heartbeat/tasks.yaml` + sets cron via `crontab -e`:

```cron
*/5 * * * * jc-heartbeat --instance-dir <path> run commitments_tick
```

### State directories

Created by `jc-init` and on first tick if missing:

```
state/commitments/             # pending (active YAML files at root)
state/commitments/done/        # executed (archived)
state/commitments/failed/      # 3 retries exhausted
```

---

## Subsystem 2 — `jc-reengage`

### Module

- `lib/reengage/`
  - `lib/reengage/conf.py` — load + validate `ops/reengage.yaml`
  - `lib/reengage/detector.py` — silence detection per tracked chat
  - `lib/reengage/queuer.py` — translate silence state into `jc-commitments add` calls
- Heartbeat builtin: `lib/heartbeat/builtins/reengage_tick.py`

No standalone binary in v1 — reengage runs only as a heartbeat builtin. Operators interact through `ops/reengage.yaml` + the resulting commitments.

### Config: `ops/reengage.yaml`

```yaml
enabled: false                # default off — operator opts in per-instance
scan_interval_hours: 6        # heartbeat cron cadence; runner reads this

# Silence handling
silence_threshold_hours: 48   # first touch never fires before this
max_touches: 4                # hard cap per silence episode
touch_schedule:               # hours since last inbound, must be ascending
  - 48
  - 72
  - 96
  - 120

# Time-of-day gating (instance timezone)
allowed_slots:                # local-time hours when touches may fire
  - "07:00"
  - "19:00"
quiet_hours:                  # never fire in this window (overrides allowed_slots)
  start: "23:00"
  end: "07:00"

# Tracked chats
tracked_chats:
  - chat_id: 28547271
    name: "Luca DM"
    # Per-touch templates live in memory/L2/templates/re-engagement/
    # If absent, agent generates at dispatch via brain call (slower, fresh content)
    templates:
      touch_1: "re-engagement/luca-touch-1.md"
      touch_2: "re-engagement/luca-touch-2.md"
      touch_3: "re-engagement/luca-touch-3.md"
      touch_4: "re-engagement/luca-touch-4.md"
```

### Detection algorithm (per tick)

For each `tracked_chats[i]`:

1. **Read last inbound timestamp.**
   Open `state/transcripts/<chat_id>.jsonl`, scan from tail, find latest line with `role=user`. Extract `ts`. If file missing or no user lines → skip this chat.
2. **Compute silence delta.** `delta_h = (now_utc - last_user_ts).total_hours()`
3. **Skip if `delta_h < silence_threshold_hours`** (still in active conversation window).
4. **Reset on fresh inbound.** Cancel any pending re-engagement commitments for this chat: `jc-commitments cancel --tag re-engagement:<chat_id>`. This is the safety net if the user replies between ticks. (Gateway hook is the better path — see Open Question #3.)
5. **Determine next touch.** Look at executed + pending commitments for `chat_id`:
   - Count commitments tagged `re-engagement:<chat_id>` with origin `reengage`
   - `touch_n = count + 1`
   - If `touch_n > max_touches`: skip (silence episode capped)
6. **Schedule the touch.**
   - Target time: next `allowed_slots` after `last_user_ts + touch_schedule[touch_n - 1]` hours, respecting `quiet_hours`
   - Generate text:
     - If `tracked_chats[i].templates.touch_<n>` is set → read the template file from `memory/L2/<path>`
     - Else → leave `text` field empty, set `metadata.generate_at_dispatch: true` — engine dispatcher calls brain at fire-time (out of scope for v1; v1 requires templates)
   - `jc-commitments add <auto-slug> --due <target> --action telegram-send --chat-id <id> --text <body> --tags "re-engagement,re-engagement:<chat_id>,touch:<n>" --origin reengage`

### Touch text — v1 policy

V1 ships with **templates required**. If `templates.touch_<n>` is unset for a tracked chat, reengage skips that chat and logs a warning. Brain-call-at-dispatch is deferred to v2 once we have a clear policy on freshness vs. predictability (Open Question #1).

Templates are markdown files under `memory/L2/templates/re-engagement/`. The agent (not framework) authors them, so they carry persona voice. Framework ships only the directory + a README explaining the convention.

### Behavioral coupling — RULES.md §24

The framework `jc-init` template adds `§24 RE-ENGAGEMENT` to `memory/L1/RULES.md` (separate PR, this spec only references it). §24 documents the *tone ladder* (touch 1 playful → touch 4 clean close) and the *hard stops* (max 4, reset on inbound). Reengage code enforces the structural rules; RULES.md §24 governs the content the agent puts in templates.

---

## Coordination & invariants

1. **Single source of truth for pending state:** `state/commitments/*.yaml`. Reengage does not maintain its own pending queue.
2. **Cancellation:** `jc-commitments cancel --tag re-engagement:<chat_id>` is the only correct way to clear pending touches on inbound reply.
3. **Idempotency:** reengage tick may run multiple times before a touch fires; the duplicate-detection by tag prevents double-queueing.
4. **Time zone discipline:** all `due_at` carry explicit TZ offsets. Engine compares in UTC. `allowed_slots` / `quiet_hours` use the instance TZ from `.env` (`TZ` var; falls back to system).
5. **Gateway-side reset (preferred):** `lib/gateway/runtime.py` `event_complete()` should call `jc-commitments cancel --tag re-engagement:<chat_id>` whenever an inbound event from a tracked chat completes. This makes reset immediate (within seconds), not eventually-consistent (next reengage tick). Spec lists this as v1 work; if it's risky, fall back to reengage-only reset and accept up to `scan_interval_hours` delay.

---

## Migration

1. **Existing Mario instance** keeps its `ops/commitments-tick.py` working until first JC release with `jc-commitments`. The `2026.05.12.01` release hook, run automatically by `jc update`, does the instance migration:
   - Reads existing `state/commitments/*.yaml` (Mario's format is already close)
   - Normalizes to v1 schema (add missing fields, validate)
   - Disables old crontab line, enables new heartbeat builtin in `heartbeat/tasks.yaml`
   - Removes `ops/commitments-tick.py` if it's an exact copy of the legacy template (sha256 match)
2. **New instances** get `state/commitments/{,done/,failed/}` from `jc-init` and the disabled `commitments_tick` builtin in `tasks.yaml`. Opt-in.
3. **Reengage:** strictly opt-in. Default `ops/reengage.yaml` ships with `enabled: false`.

---

## Test plan

### Unit
- `lib/commitments/schema.py` — YAML parse, validation, schema edge cases
- `lib/commitments/engine.py` — tick logic with mocked clock and mocked action handler
- `lib/commitments/actions.py` — telegram_send with mocked sender; failure paths
- `lib/reengage/detector.py` — silence computation with synthetic transcripts at various ages
- `lib/reengage/queuer.py` — duplicate-detection, touch sequencing, time-of-day gating

### Integration
- End-to-end: write a commitment YAML with `due_at = now + 30s`, run `jc-commitments tick`, assert telegram_send called once, file moved to `done/`
- Reset path: enqueue 3 re-engagement touches, simulate inbound, assert all 3 cancelled by gateway hook (or by next reengage tick)

### Soak
- 24h on a test instance: synthetic silence at 48h boundary, observe correct touch firing in allowed slots only

### DKIM / approval
- Re-engage is a *producer* — no changes to RULES.md or IDENTITY.md. No DKIM gate needed.
- Commitments engine fires Telegram messages — same approval surface as existing heartbeat tasks. Operator owns risk via `enabled: false` default + per-chat opt-in.

---

## Out of scope (v1)

- Brain-call-at-dispatch (generate touch text fresh at fire time)
- `email-send` action handler
- Cross-instance jc-event commitments (schema-ready, no consumers yet)
- Web UI / dashboard for commitment review
- SQLite-backed pending store (current file-per-commitment design is fine up to ~1k active; revisit at scale)
- Group-chat re-engagement (v1 tracks DMs only — group silence semantics need their own design)

---

## Open questions

1. **Touch text source.** V1 = templates required. V2 = brain-call-at-dispatch optional. Is that the right phasing, or should v1 support both?
2. **Sweep runner (§25).** Separate spec or fold into this one? Sweep is structurally similar (periodic audit producing reports) but its product is a markdown report, not a commitment. Recommendation: separate spec, share no code with this one.
3. **Gateway-side reset hook.** Ship in v1 (cleaner) or defer to v2 (less framework surface change)? My take: ship in v1 — the alternative is up-to-6h drift between reply and cancellation, which makes the feature feel laggy.
4. **Allowed-slots resolution.** `07:00` and `19:00` is Luca-specific. Should this be per-tracked-chat (e.g. some chats fire any-time, some are slot-gated)?
5. **Failed-touch escalation.** When a commitment moves to `failed/`, should there be a Telegram alert to the operator? Or just log + leave for next sweep?

---

## Implementation order

1. `jc-commitments` engine + schema + `telegram-send` dispatcher + heartbeat builtin
2. `jc-init` updates: scaffold `state/commitments/{,done/,failed/}`, add disabled `commitments_tick` task
3. Migration script for Mario
4. Test coverage on (1)
5. `jc-reengage` builtin + config + detector
6. Gateway-side cancellation hook
7. RULES.md §24/§25/§26 template additions (PR independent of code, can land first or last)
