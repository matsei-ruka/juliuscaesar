# Gateway

The gateway is JuliusCaesar's runtime hub: every inbound message — Telegram,
Slack, Discord, voice, scheduled cron, worker completion event — lands in a
local SQLite queue, gets routed to a brain (claude / codex / gemini /
opencode / aider), and the response goes back through the originating
channel.

This document is operator-focused: architecture, components, debugging.

## Architecture

```
INBOUND CHANNELS                 GATEWAY                          BRAIN ADAPTERS
- telegram          ┌────────►   ┌──────────────────┐    ┌────►   - claude
- slack             │            │ event queue      │    │        - codex
- discord           │            │ overrides parser │    │        - gemini
- voice (paired)   ─┤            │ triage           ├────┤        - opencode
- jc-events         │            │ router           │    │        - aider
- cron              │            │ session manager  │    │
                    └─────►      └──────────────────┘    │
                                                          │
OUTBOUND CHANNELS  ◄──────────────────────────────────────┘
```

## Components

| Component | Path                                  | Purpose                                  |
|-----------|---------------------------------------|------------------------------------------|
| Daemon    | `bin/jc-gateway`                      | Lifecycle + CLI                          |
| Runtime   | `lib/gateway/runtime.py`              | Dispatcher loop                          |
| Queue     | `lib/gateway/queue.py`                | SQLite event store                       |
| Sessions  | `lib/gateway/sessions.py`             | Per-(channel,convo,brain) session ids    |
| Router    | `lib/gateway/router.py`               | Pure decision tree                       |
| Overrides | `lib/gateway/overrides.py`            | `[brain]` prefix and `/brain` slash      |
| Channels  | `lib/gateway/channels/`               | Per-channel modules                      |
| Brains    | `lib/gateway/brains/`                 | Per-brain Python wrappers                |
| Triage    | `lib/gateway/triage/`                 | ollama / openrouter / claude-channel     |
| Context   | `lib/gateway/context.py`              | L1 memory preamble loader                |
| Logging   | `lib/gateway/logging_setup.py`        | Rotating JSON logs                       |
| Metrics   | `state/gateway/triage-metrics.db`     | Class counts, confidence, fallback rate  |

## Lifecycle

```
jc gateway init       # create queue.db
jc gateway start      # detached daemon
jc gateway stop
jc gateway restart
jc gateway run        # foreground (debug)
jc gateway reload     # SIGHUP, swap config + triage backend without restart
jc gateway status     # pid, queue depth, recent events
jc gateway tail       # plain-text tail
jc gateway logs --since 10m --class analysis --brain claude:opus-4-7-1m
jc gateway metrics --hours 24
```

## Event flow (Telegram → Opus analysis)

1. `lib/gateway/channels/telegram.py` long-polls Bot API.
2. Inbound message becomes a queue event with `(source="telegram",
   source_message_id, conversation_id)`.
3. Dispatcher claims the event, parses any `[opus]` prefix or `/brain X`
   slash.
4. Router decides: override > cron-pinned > sticky > triage > fallback >
   channel default.
5. Brain wrapper invokes the matching shell adapter, captures the new native
   session id, returns the response.
6. Sticky table updates so the next message in this conversation reuses the
   same brain until `sticky_brain_idle_timeout_seconds` elapses.
7. `lib/gateway/channels/registry.deliver()` sends the response back through
   the originating transport.

## Reliability

- **Backpressure.** When `queued + running >= max_queue_depth` (default 100),
  new events are dropped with a warning log.
- **Brain timeout.** Each brain has `brains.<name>.timeout_seconds`; on
  timeout the subprocess group is SIGTERM'd then SIGKILL'd.
- **Retries.** Failed events retry with `event_retry_backoff_seconds`
  (default `[10, 60, 300]`).
- **Log rotation.** `state/gateway/gateway.log` rotates at 50MB, 5 backups.
- **Idempotent inbound.** `(source, source_message_id)` unique index gives
  free at-most-once for replays.

## Debugging cheat sheet

| Symptom                                    | First thing to check                                  |
|--------------------------------------------|-------------------------------------------------------|
| `jc gateway status` shows daemon stopped   | Watchdog config: `RUNTIME_MODE=gateway`?              |
| Telegram delivers but no Rachel reply      | `jc gateway logs --since 10m` — adapter timeouts?     |
| Smalltalk hits Opus                        | `jc gateway metrics` — triage confidence, then routes |
| Slack 409                                  | `pip install websocket-client`; one Slack app per ws  |
| Discord channel idle                       | `pip install discord.py`; intent on; bot in server    |
| Worker completes but no Telegram message   | `state/events/` accumulating? jc-events disabled?     |

## Invariants

- No `--channels` flag in adapter shell scripts under gateway mode. The
  legacy live-Claude path (`RUNTIME_MODE=legacy-claude`) is still supported
  but deprecated.
- No brain or network I/O inside SQLite transactions.
- Per-`(channel, user_id)` serialization at the queue worker.
- `state/` belongs to the instance and is ignored by templates.
