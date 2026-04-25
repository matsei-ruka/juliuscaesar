---
title: cron gateway channel
section: subsystem
status: active
code_anchors:
  - path: lib/gateway/channels/cron.py
    symbol: "class CronChannel:"
last_verified: 2026-04-25
verified_by: claude
related:
  - subsystem/gateway-queue.md
  - subsystem/heartbeat-runner.md
---

## Summary

The `cron` channel bridges scheduled heartbeat tasks into the gateway queue.
With this channel enabled, hash-delta-tripped tasks emit a JSON file under
`<instance>/state/cron/` that the channel turns into a queue event with
`meta.brain` pinned to the task's chosen brain.

## Schema

```json
{
  "task_name": "morning_briefing",
  "run_id": "morning-2026-04-25",
  "prompt": "Summarize the news.",
  "brain": "claude",
  "model": "opus-4-7-1m",
  "notify_channel": "telegram",
  "notify_chat_id": "12345"
}
```

## Behavior

- `conversation_id` is `cron:<task_name>` so the same task resumes its own
  brain session across runs.
- `meta.brain` is the canonical `<brain>:<model>` spec consumed by the
  router's `cron_pinned` branch — sticky and triage are skipped.
- Standalone heartbeat (`jc heartbeat run --once`) still works without this
  channel; cron mode is opt-in.

## Invariants

- Cron tasks always bypass triage (deterministic destinations).
- `state/cron/` is created on demand and ignored by templates.
