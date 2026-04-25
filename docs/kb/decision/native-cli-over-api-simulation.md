---
title: Native CLI orchestration instead of API simulation
section: decision
status: active
code_anchors:
  - path: README.md
    symbol: "No API simulation, no session spoofing, no TOS concerns."
  - path: docs/ARCHITECTURE.md
    symbol: "JC never simulates Claude Code."
  - path: lib/heartbeat/adapters/claude.sh
    symbol: "Use subscription auth"
last_verified: 2026-04-25
verified_by: l.mattei
related:
  - domain/personal-assistant-framework.md
  - subsystem/watchdog-runtime.md
  - contract/adapter-and-delivery-contracts.md
---

## Decision

JuliusCaesar invokes native assistant CLIs, especially the real `claude` CLI, instead of simulating Claude Code over an API or injecting subscription credentials into a custom API client.

## Rationale

The project borrows the daemon architecture from OpenClaw but avoids API simulation. Users authenticate through each tool's official login flow, and JuliusCaesar orchestrates subprocesses around those tools.

For Claude Code, scheduled or worker jobs use `claude -p` in fresh non-interactive sessions. The live Telegram-bound Claude session remains separate and is supervised by watchdog.

## Consequences

Positive:

- Subscription-bound usage remains on the user's own subscription.
- Framework code avoids session spoofing and API-simulation instability.
- Tool upgrades are largely absorbed through the native CLI interface.
- Multiple brains can share a simple adapter contract.

Tradeoffs:

- Runtime depends on local CLI availability and login state.
- CLI behavior changes can break adapters.
- Some features require process supervision rather than API-level orchestration.
- Diagnostics must inspect local binaries, processes, and per-tool session stores.

## Invariants

- Framework adapters call native tools as subprocesses.
- Adapter auth is delegated to each tool.
- The live Claude session is not reused for scheduled heartbeat synthesis.
- Watchdog restarts the live session rather than pretending to own an API session.

## Open questions / known stale

- 2026-04-25: This decision is clear for Claude Code. Long-term behavior for other brains may evolve as their CLIs change.
