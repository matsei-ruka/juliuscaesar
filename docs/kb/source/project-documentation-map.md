---
title: Project documentation map
section: source
status: active
code_anchors:
  - path: README.md
    symbol: "Components (shipped)"
  - path: QUICKSTART.md
    symbol: "From zero to a running JuliusCaesar instance"
  - path: docs/ARCHITECTURE.md
    symbol: "Process model"
  - path: ROADMAP.md
    symbol: "0.2.0"
last_verified: 2026-04-25
verified_by: l.mattei
sources:
  - path: README.md
    title: README
  - path: QUICKSTART.md
    title: Quickstart
  - path: docs/ARCHITECTURE.md
    title: Architecture
  - path: ROADMAP.md
    title: Roadmap
  - path: docs/specs/workers.md
    title: Workers spec
  - path: docs/specs/named-workers.md
    title: Named workers spec
related:
  - domain/personal-assistant-framework.md
  - subsystem/workers-background-agents.md
---

## Summary

The docs split by audience and stability:

- `README.md`: project pitch, shipped components, contracts, quick start, and architecture pointer.
- `QUICKSTART.md`: end-to-end setup using `jc setup`, from machine prerequisites to gateway runtime, Telegram/Slack, voice, heartbeat, workers, watchdog, and troubleshooting.
- `docs/ARCHITECTURE.md`: compact system model and component relationships.
- `ROADMAP.md`: shipped milestones and future work.
- `docs/specs/workers.md`: design spec for on-demand background workers.
- `docs/specs/named-workers.md`: design spec for persistent worker identities and resume behavior.

## Current shipped picture

README marks 0.3.0 production gateway work in progress and presents `jc setup` plus `jc gateway` as the main runtime path. Quickstart now treats Telegram and Slack Socket Mode as gateway channels and keeps the old Claude Telegram plugin path as legacy fallback.

## Where to look first

- Need user setup steps: `QUICKSTART.md`.
- Need conceptual architecture: `docs/ARCHITECTURE.md`.
- Need exact binary behavior: `bin/jc-*` files.
- Need memory database behavior: `lib/memory/db.py`.
- Need scheduled task behavior: `lib/heartbeat/runner.py`.
- Need worker behavior: `bin/jc-workers` plus `lib/workers/db.py`.

## Open questions / known stale

- 2026-04-25: Specs can be ahead of implementation. Verify code before relying on a spec detail.
