---
title: Why a unified gateway
section: decision
status: active
code_anchors:
  - path: lib/gateway/runtime.py
    symbol: "class GatewayRuntime:"
  - path: lib/gateway/router.py
    symbol: "def route("
  - path: docs/specs/unified-gateway-0.3.0-remaining.md
    symbol: "# Spec: Unified Gateway"
last_verified: 2026-05-01
verified_by: l.mattei
related:
  - subsystem/gateway-queue.md
  - decision/native-cli-over-api-simulation.md
---

## Decision

JuliusCaesar 0.3.0 adopts a single long-running daemon (the gateway) that
owns every inbound and outbound channel, runs every brain non-interactively
per event, and applies optional triage to pick the right brain per message.
This replaces the 0.2.x model where a single Telegram MCP plugin owned the
live channel and Claude Code was the only supported live brain.

## Rationale

Three pain points drove the change:

1. **Single point of failure.** Telegram plugin death = no inbound and no
   outbound. The 0.2.x watchdog restarted cleanly but the underlying coupling
   remained: one plugin per channel per brain.
2. **Worker → user friction.** Worker completions sent a raw "worker #N done"
   to Telegram. The user had to ask for a summary. Two messages to learn one
   fact.
3. **Brain lock-in.** Heartbeat already shelled out to native CLIs, but the
   interactive layer was Claude-only. External users without a Claude
   subscription had no working setup.

Layered on top: every message — small talk and deep analysis alike — paid
full Sonnet/Opus pricing. There was no triage.

## Consequences

Positive:

- One daemon, one liveness path, one supervision target.
- Channels are pluggable. Telegram + Slack + Discord + voice work
  simultaneously today. Webhook channels are straightforward to add.
- Brains are pluggable. Default brain per channel, per-message override
  (`[opus] ...`), sticky-brain per conversation, slash command (`/brain X`).
- Triage trims cost: smalltalk routes to Haiku, analysis to Opus, with a
  confidence threshold and graceful fallback.
- Worker completions auto-synthesize via `jc-events` — single message UX.

Tradeoffs:

- Bigger Python codebase to maintain than a thin MCP plugin.
- Multiple optional dependencies (`websocket-client`, `discord.py`, `httpx`).
  Each is gated and the gateway boots without them.
- Triage adds 50–3000 ms latency per inbound message depending on backend.
- The `claude-channel` triage backend keeps a second Claude session warm —
  modest memory cost.

## Invariants

- No `--channels` flag in gateway-mode adapter shell scripts. Channel
  ownership is the gateway's, not Claude's.
- No brain or network I/O inside a SQLite transaction.
- Per-`(channel, user_id)` serialization keeps conversations coherent.
- Users without triage configuration get the channel default brain — the
  router falls through cleanly.

## Open questions / known stale

- 2026-04-25: Memory updates from gateway brains (a `<memory_update>` markup
  contract) is still 0.3.1 work.
- 2026-04-25: Voice as a transparent transformer (rather than paired channel)
  remains an open option for 0.4.0.
- 2026-05-01: Spec moved from `unified-gateway-0.3.0.md` to
  `unified-gateway-0.3.0-remaining.md` — most of the original spec shipped.
  Subsystems beyond the original spec now live in-tree:
  `lib/gateway/triage`, `lib/gateway/recovery`, `lib/gateway/transcripts`,
  `lib/gateway/process_sessions`, `lib/gateway/sessions.py`, sender approval,
  and Slack Socket Mode.
