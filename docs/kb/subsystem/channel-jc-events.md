---
title: jc-events internal channel
section: subsystem
status: active
code_anchors:
  - path: lib/gateway/channels/jc_events.py
    symbol: "class JcEventsChannel:"
  - path: bin/jc-workers
    symbol: "def _write_worker_event"
last_verified: 2026-05-01
verified_by: l.mattei
related:
  - subsystem/gateway-queue.md
  - subsystem/workers-background-agents.md
---

## Summary

The `jc-events` channel watches `<instance>/state/events/*.json` and enqueues a
gateway event for each file it finds. Used to bring worker / system / watchdog
events into the gateway pipeline so the configured brain can synthesize a
single user-facing message instead of two.

## Schema

A JSON file dropped under `state/events/` must be a JSON object with at least:

```json
{
  "event_type": "worker.completed",
  "event_id": "worker-18-done",
  "worker_id": 18,
  "topic": "fix bugs",
  "status": "done",
  "duration_seconds": 145,
  "result_path": "state/workers/18/result",
  "notify_channel": "telegram",
  "notify_chat_id": "28547271"
}
```

`event_type=worker.completed` produces a synthesis prompt template; other
types fall back to a generic "summarize if useful" prompt.

## Behavior

- The channel polls every `poll_interval_seconds` (default 2).
- A successfully processed file is deleted.
- A file that fails JSON parsing is renamed `<file>.bad` so it does not loop.
- `notify_channel` becomes `meta.delivery_channel` so the synthesis lands in
  the right transport (`telegram` / `slack` / `discord`).

## Producer: jc-workers

`bin/jc-workers` writes `worker-<id>-<status>.json` on terminal state.
Override with `WORKERS_NOTIFY_MODE`:

| Value             | Behavior                                                    |
|-------------------|-------------------------------------------------------------|
| `auto` (default)  | events when gateway config exists, else telegram-direct     |
| `events`          | always write event JSON (requires gateway)                  |
| `gateway-deliver` | enqueue raw body with `deliver_only=true`, no synthesis     |
| `telegram-direct` | invoke `lib/heartbeat/lib/send_telegram.sh` directly        |

## Invariants

- Channel never deletes outside `state/events/`.
- Files are atomic-renamed before deletion so partial writes are not consumed.
