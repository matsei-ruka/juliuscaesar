---
title: JuliusCaesar personal assistant framework
section: domain
status: active
code_anchors:
  - path: README.md
    symbol: "JuliusCaesar takes the architecture and runs it on the real `claude` CLI"
  - path: docs/ARCHITECTURE.md
    symbol: "Two-repo model"
last_verified: 2026-04-25
verified_by: l.mattei
related:
  - decision/native-cli-over-api-simulation.md
  - contract/instance-layout-and-resolution.md
---

## Summary

JuliusCaesar is an OpenClaw-inspired personal assistant framework that runs native assistant CLIs around a user-owned instance directory. The framework repo provides reusable tooling: scheduler, supervisor, memory CLI, voice, installer, workers, and diagnostics. It is not supposed to contain user identity, secrets, or memory content.

The core product shape is daemon-like rather than chat-app-like: persistent memory, cron-driven work, Telegram delivery, watchdog supervision, and optional on-demand workers.

## Source of truth

- Framework code lives in this repo.
- Instance data lives in a separate private repo or directory owned by the user.
- An instance is the runtime workspace. `jc setup` configures it for first run, `jc init` is the low-level scaffold, and `jc <subcommand>` resolves and operates on it.
- Framework binaries are installed globally as `jc-*` shims, but they point back to the checked-out framework repo.

## Operational model

Typical bootstrapping:

1. Install framework with `./install.sh`.
2. Configure an instance with `jc setup <path>`.
3. Review `<instance>/.env` and L1 memory files.
4. Rebuild memory with `jc memory rebuild`.
5. Validate with `jc doctor`.
6. Run scheduled tasks with `jc heartbeat run`, live Telegram via Claude Code, and optional watchdog.

## Invariants

- User data belongs in the instance, not the framework.
- Secrets belong in `<instance>/.env`, mode 600.
- SQLite indexes are derived state, not source of truth.
- Tool authentication is handled by each native CLI, not by framework-side session spoofing.

## Open questions / known stale

- 2026-04-25: Roadmap says skill loading, docs site, config schema validation, and CI remain future or partial work.
