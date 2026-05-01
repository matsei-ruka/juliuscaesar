---
title: Project documentation map
section: source
status: active
code_anchors:
  - path: README.md
    symbol: "## Architecture"
  - path: QUICKSTART.md
    symbol: "From zero to a running JuliusCaesar instance"
  - path: docs/ARCHITECTURE.md
    symbol: "## Process model"
  - path: ROADMAP.md
    symbol: "0.2.0"
last_verified: 2026-05-01
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
  - path: docs/specs/unified-gateway-0.3.0-remaining.md
    title: Unified gateway 0.3.0 remaining work
related:
  - domain/personal-assistant-framework.md
  - subsystem/workers-background-agents.md
---

## Summary

The docs split by audience and stability:

- `README.md`: marketing-positioned pitch, competitive positioning vs OpenClaw / Hermes, design contracts, quick start. Reorganized 2026-04-28 (commit 17a5ece).
- `QUICKSTART.md`: end-to-end setup using `jc setup`, from machine prerequisites to gateway runtime, Telegram/Slack, voice, heartbeat, workers, watchdog, and troubleshooting.
- `docs/ARCHITECTURE.md`: compact system model and component relationships.
- `ROADMAP.md`: shipped milestones and future work.
- `docs/specs/`: ~19 specs covering workers, named workers, gateway-sender-approval, telegram-* (chat-discovery, group-auth/context/mentions, multimedia, slash-commands, md-rewriter), conversation-transcripts, voice-reply-path, autonomous-user-model, codex-auth-extractor, hot-md-structure, process-hygiene, and unified-gateway-0.3.0-remaining.

## Current shipped picture

README is now marketing-positioned (vs OpenClaw / Hermes) and lists CalVer baseline `2026.04.28`. Gateway is the production path; legacy Claude Telegram plugin remains as `RUNTIME_MODE=legacy-claude` fallback. Slack Socket Mode and Discord both ship in 0.3.x.

## Where to look first

- Need user setup steps: `QUICKSTART.md`.
- Need conceptual architecture: `docs/ARCHITECTURE.md`.
- Need exact binary behavior: `bin/jc-*` files.
- Need memory database behavior: `lib/memory/db.py`.
- Need scheduled task behavior: `lib/heartbeat/runner.py`.
- Need worker behavior: `bin/jc-workers` plus `lib/workers/db.py`.

## Open questions / known stale

- 2026-04-25: Specs can be ahead of implementation. Verify code before relying on a spec detail.
- 2026-05-01: `unified-gateway-0.3.0.md` spec was renamed to `unified-gateway-0.3.0-remaining.md` (scope reduced to remaining work). Anchors elsewhere in KB may still reference the old name.
