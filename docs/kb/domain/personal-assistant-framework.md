---
title: JuliusCaesar personal assistant framework
section: domain
status: active
code_anchors:
  - path: README.md
    symbol: "## Why JuliusCaesar"
  - path: docs/ARCHITECTURE.md
    symbol: "## Two-repo model"
last_verified: 2026-05-01
verified_by: l.mattei
related:
  - decision/native-cli-over-api-simulation.md
  - contract/instance-layout-and-resolution.md
---

## Summary

JuliusCaesar is an OpenClaw-inspired personal assistant framework that runs native assistant CLIs around a user-owned instance directory. The framework repo provides reusable tooling: scheduler, supervisor, memory CLI, voice, installer, workers, and diagnostics. It is not supposed to contain user identity, secrets, or memory content.

The core product shape is daemon-like rather than chat-app-like: persistent memory, cron-driven work, Telegram/Slack gateway delivery, watchdog supervision, and optional on-demand workers.

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
6. Start `jc gateway`, install watchdog if desired, and run scheduled tasks with `jc heartbeat run`.

## Invariants

- User data belongs in the instance, not the framework.
- Secrets belong in `<instance>/.env`, mode 600.
- SQLite indexes are derived state, not source of truth.
- Tool authentication is handled by each native CLI, not by framework-side session spoofing.

## Open questions / known stale

- 2026-04-25: Roadmap says skill loading, docs site, config schema validation, and CI remain future or partial work.
- 2026-05-01: 2026.04.28 CalVer baseline shipped; `jc update` performs in-place framework upgrades. `jc upgrade` reconfigures an existing instance's channels/brain/triage. New runtime subsystems (`lib/gateway/triage`, `lib/gateway/recovery`, `lib/gateway/transcripts`, `lib/gateway/process_sessions`, `lib/company/`, `bin/jc-user-model`) ship in 0.3.x but are not yet covered by their own KB entries.
