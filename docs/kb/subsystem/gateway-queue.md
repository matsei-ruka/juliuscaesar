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
last_verified: 2026-05-01
verified_by: l.mattei
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
- `lib/gateway/channels/`: per-channel clients — `telegram.py` (split: `telegram_chats.py`, `telegram_commands.py`, `telegram_media.py`, `telegram_outbound.py`, `telegram_routing.py`), `slack.py`, `discord.py`, `voice.py`, `cron.py`, `jc_events.py`, plus `registry.py`/`base.py`/`channel_lifecycle.py`.
- `lib/gateway/brain.py` + `lib/gateway/brains/<name>.py`: per-brain Python wrappers; legacy `brain.py` is the fallback path for brains without a wrapper.
- `lib/gateway/router.py`: per-event brain selection (default → cron-pinned → triage → sticky → override).
- `lib/gateway/triage/`: pluggable triage backends — `claude_channel`, `codex_api`, `ollama`, `openrouter`, plus `cache` and `metrics`.
- `lib/gateway/recovery/`: failure classifier + per-handler recovery (e.g. session-drop on `--resume <expired-uuid>`).
- `lib/gateway/sessions.py` + `lib/gateway/process_sessions.py`: per-`(channel, conversation_id, brain)` session map and live-process tracking.
- `lib/gateway/transcripts.py`: per-conversation chat transcripts (read by `bin/jc-transcripts`).
- `lib/gateway/chats.py`: Telegram chat directory + auth status (read by `bin/jc-chats`).
- `lib/gateway/format/escaper.py`: Telegram MarkdownV2 rewriter.
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

- 2026-04-25: Public webhook channel and richer voice integration remain roadmap work. Telegram, Slack Socket Mode, Discord, voice, jc-events, and cron channels ship in 0.3.0.
- 2026-05-01: No standalone KB entries yet for `lib/gateway/triage/`, `lib/gateway/recovery/`, `lib/gateway/sessions.py`, `lib/gateway/transcripts.py`, sender approval flow, or the Slack channel — they ship but live as one-paragraph notes here. Worth promoting to their own entries on next pass.
