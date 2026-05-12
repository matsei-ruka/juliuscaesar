---
title: Commitments and re-engagement
section: subsystem
status: active
code_anchors:
  - path: lib/commitments/schema.py
    symbol: "class Commitment:"
  - path: lib/commitments/engine.py
    symbol: "def tick("
  - path: lib/reengage/conf.py
    symbol: "class ReengageConfig:"
  - path: lib/reengage/queuer.py
    symbol: "def cancel_if_tracked"
  - path: bin/jc-commitments
    symbol: "jc-commitments"
last_verified: 2026-05-12
verified_by: Matsei Ruka
related:
  - subsystem/heartbeat-runner.md
  - subsystem/gateway-queue.md
---

## Summary

Commitments are durable future actions stored as YAML files under
`state/commitments/*.yaml`. `jc commitments tick` scans pending files, fires
due actions, archives successful one-shot commitments to `done/`, advances
daily/weekly repeats, and moves exhausted or permanent failures to `failed/`.

Re-engagement is a producer of commitments, not a separate delivery system. It
reads `ops/reengage.yaml`, checks tracked chat transcripts for silence, and
queues template-backed Telegram commitments when a silence threshold is crossed.
V1 requires templates under `memory/L2/templates/re-engagement/`; it does not
generate fresh text at dispatch time.

## Runtime flow

1. Producers write `state/commitments/<slug>.yaml` through `jc commitments add`
   or library calls.
2. `commitments_tick` runs from heartbeat and calls `commitments.engine.tick`.
3. Due commitments dispatch through registered action handlers.
4. `telegram-send` uses the canonical Telegram sender/escaping path.
5. `jc-event` writes JSON into `state/events/` for the gateway `jc-events`
   channel to synthesize or route.

## Re-engagement rules

- `ops/reengage.yaml` is disabled by default.
- Each tracked chat has a `chat_id`, optional per-chat slots, and template
  paths for `touch_1` through `touch_4`.
- Only one pending re-engagement commitment may exist for a chat at once.
- Any active inbound reply cancels pending touches through
  `cancel_if_tracked`.
- Touch count is capped by `max_touches`; default is 4.

## Invariants

- `due_at` and `created_at` must be timezone-aware ISO-8601 strings.
- Pending commitments live only at the root of `state/commitments/`; archived
  files live in `done/` or `failed/`.
- Re-engagement cancellation uses the tag `re-engagement:<chat_id>`.
- The framework controls queueing/canceling. Persona-owned templates control
  the words.
- Disabled heartbeat builtins run as dry-runs, so operators can schedule cron
  before flipping `enabled: true`.

## Open questions / known stale

- 2026-05-12: No `email-send` action handler yet; the schema is ready but v1
  ships Telegram and `jc-event` only.
