---
title: Gateway runtime and event queue
section: subsystem
status: active
code_anchors:
  - path: bin/jc-gateway
    symbol: "def build_parser() -> argparse.ArgumentParser:"
  - path: lib/gateway/queue.py
    symbol: "def claim_next("
  - path: lib/gateway/runtime.py
    symbol: "class GatewayRuntime:"
last_verified: 2026-04-25
verified_by: codex
related:
  - subsystem/installation-and-cli-routing.md
  - contract/instance-layout-and-resolution.md
---

## Summary

The gateway uses a local SQLite queue at `<instance>/state/gateway/queue.db`. It is the durable handoff point between channel adapters, worker/system events, brain invocations, and outbound delivery.

The production runtime supports Telegram long polling, Slack Socket Mode, queue-backed dispatch, adapter invocation through the shared brain scripts, retries, delivery, and session continuity.

## Components

- `lib/gateway/queue.py`: SQLite backend and event state transitions.
- `lib/gateway/runtime.py`: dispatcher loop that claims events and runs/delivers work.
- `lib/gateway/channels.py`: Telegram and Slack Socket Mode clients.
- `lib/gateway/brain.py`: shared adapter invocation and session capture.
- `bin/jc-gateway`: CLI for queue inspection, daemon lifecycle, logs, config, events, and retry.
- `<instance>/state/gateway/queue.db`: durable queue database.
- `<instance>/ops/gateway.yaml`: non-secret runtime config.
- `<instance>/state/gateway/jc-gateway.pid`: daemon PID file.
- `<instance>/state/gateway/gateway.log`: daemon log.

## Queue Semantics

- Events start as `queued`.
- A worker uses `claim_next()` to move one ready event to `running` with `locked_by` and `locked_until`.
- Brain/channel work must happen after the claim transaction commits.
- `complete()` marks an event `done` and stores the response.
- `fail()` retries with delayed backoff until `max_retries`, then marks the event `failed`.
- Expired `running` leases are returned to `queued` during claim.
- Dedup uses `(source, source_message_id)` when the source provides a stable message id.
- Sessions are stored by `(channel, conversation_id, brain)` so later messages resume the same native brain conversation when an adapter exposes a session id.

## CLI Surface

`jc gateway` supports:

- `init`
- `start`
- `stop`
- `restart`
- `run` for foreground debugging
- `status`
- `tail`
- `logs`
- `enqueue`
- `claim`
- `complete`
- `fail`
- `list`
- `events`
- `retry`
- `config`
- `work-once` for local smoke testing with an echo worker

## Invariants

- SQLite transactions stay short.
- No brain invocation or network channel I/O happens inside a SQLite transaction.
- The queue database is runtime state and belongs under `<instance>/state/`.
- The daemon performs queue maintenance, channel polling, dispatch, and delivery.
- `state/` is ignored by newly initialized instances.
- Slack Socket Mode requires the optional `websocket-client` Python package.

## Open questions / known stale

- 2026-04-25: Discord and public webhook channels are still roadmap work.
