---
title: Gateway event queue
section: subsystem
status: active
code_anchors:
  - path: bin/jc-gateway
    symbol: "def build_parser() -> argparse.ArgumentParser:"
  - path: lib/gateway/queue.py
    symbol: "def claim_next("
last_verified: 2026-04-25
verified_by: codex
related:
  - subsystem/installation-and-cli-routing.md
  - contract/instance-layout-and-resolution.md
---

## Summary

The gateway foundation uses a local SQLite queue at `<instance>/state/gateway/queue.db`. It is the durable handoff point for future channel adapters, worker/system events, and brain invocations.

This first slice is queue and lifecycle only: it provides schema creation, enqueue, claim, complete, fail/retry, status, recent-event inspection, and a minimal daemon maintenance loop. Channel adapters and brain invocation will build on this API.

## Components

- `lib/gateway/queue.py`: SQLite backend and event state transitions.
- `bin/jc-gateway`: CLI for initializing and inspecting the queue, plus daemon lifecycle.
- `<instance>/state/gateway/queue.db`: durable queue database.
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

## CLI Surface

`jc gateway` supports:

- `init`
- `start`
- `stop`
- `restart`
- `run` for foreground debugging
- `status`
- `tail`
- `enqueue`
- `claim`
- `complete`
- `fail`
- `list`
- `work-once` for local smoke testing with an echo worker

## Invariants

- SQLite transactions stay short.
- No brain invocation or network channel I/O happens inside a SQLite transaction.
- The queue database is runtime state and belongs under `<instance>/state/`.
- The daemon currently performs queue maintenance only, including requeueing expired leases.
- `state/` is ignored by newly initialized instances.

## Open questions / known stale

- 2026-04-25: Channel adapters and real brain invocation are not implemented yet.
