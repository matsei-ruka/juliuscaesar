# Knowledge Base - Operation Log

Append-only audit trail. Newest at bottom.

Format: `## [YYYY-MM-DD] <verb> | <target> - <one-line note>`

---

## [2026-04-25] init | docs/kb - scaffolded KB config and index
## [2026-04-25] build | docs/kb - initial verified map of framework docs, binaries, and libraries
## [2026-04-25] update | subsystem/installation-and-cli-routing.md - added jc setup first-run configurator behavior
## [2026-04-25] update | subsystem/watchdog-runtime.md - documented orphan telegram plugin restart handling
## [2026-04-25] update | contract/config-and-secret-boundaries.md - documented safe env parsing and setup secret handling
## [2026-04-25] update | subsystem/voice-dashscope.md - documented default voice enrollment name
## [2026-04-25] update | subsystem/watchdog-runtime.md - documented Telegram plugin ownership checks
## [2026-04-25] new | subsystem/gateway-queue.md - documented SQLite gateway queue foundation
## [2026-04-25] update | subsystem/gateway-queue.md - documented gateway daemon lifecycle and doctor fix behavior
## [2026-04-25] update | subsystem/gateway-queue.md - documented Telegram/Slack runtime dispatch and sessions
## [2026-04-25] update | subsystem/watchdog-runtime.md - documented default gateway watchdog mode
## [2026-04-25] update | contract/adapter-and-delivery-contracts.md - documented JC_RESUME_SESSION and gateway delivery
## [2026-04-25] update | subsystem/workers-background-agents.md - documented gateway-backed worker notifications
## [2026-04-25] update | source/project-documentation-map.md - documented gateway-first README and quickstart
## [2026-04-25] update | subsystem/installation-and-cli-routing.md - documented gateway production CLI and websocket dependency
## [2026-04-25] update | contract/config-and-secret-boundaries.md - documented Slack secrets and gateway config
## [2026-04-25] update | decision/native-cli-over-api-simulation.md - documented gateway-owned channels
## [2026-04-25] update | domain/personal-assistant-framework.md - documented Telegram/Slack gateway runtime model
## [2026-04-25] update | contract/instance-layout-and-resolution.md - documented ops/gateway.yaml and gateway start path
## [2026-04-25] update | subsystem/installation-and-cli-routing.md - documented router empty-arg guard and doctor venv Python probes
## [2026-04-25] update | contract/config-and-secret-boundaries.md - documented doctor venv Python diagnostic invariant
## [2026-04-27] new | subsystem/channel-telegram.md — Telegram long-poll channel, auth gate, per-cycle _chats_conn close
## [2026-05-01] update | domain/personal-assistant-framework.md — fix README anchors; note CalVer + new lib/gateway subsystems
## [2026-05-01] update | source/project-documentation-map.md — fix README anchor; expand spec list; note unified-gateway spec rename
## [2026-05-01] update | decision/native-cli-over-api-simulation.md — note codex_api direct-API exception for triage
## [2026-05-01] update | decision/why-unified-gateway.md — point spec anchor at unified-gateway-0.3.0-remaining.md
## [2026-05-01] update | contract/instance-layout-and-resolution.md — fix README anchor; document state/ subdirs
## [2026-05-01] update | contract/config-and-secret-boundaries.md — fix README anchor; list new yaml blocks (sender_approval, triage, codex_auth, company)
## [2026-05-01] update | contract/brain-capabilities.md — add codex_api row + invocation column
## [2026-05-01] update | contract/adapter-and-delivery-contracts.md — add gateway brain wrappers section; aider/minimax notes; JC_IN_WORKER
## [2026-05-01] update | subsystem/installation-and-cli-routing.md — list new bin/jc subcommands (update, upgrade, chats, transcripts, company, user-model, codex-auth, migrate-to-0.3)
## [2026-05-01] update | subsystem/memory-system.md — noindex flag, parser state validation, sync_l1_rules.py
## [2026-05-01] update | subsystem/heartbeat-runner.md — MCP enable + session continuity, snapshot-diff session capture
## [2026-05-01] update | subsystem/workers-background-agents.md — JC_IN_WORKER recursion guard
## [2026-05-01] update | subsystem/watchdog-runtime.md — verify against current code (no content drift)
## [2026-05-01] update | subsystem/voice-dashscope.md — verify against current code (no content drift)
## [2026-05-01] update | subsystem/gateway-queue.md — list channel module split; new triage/recovery/sessions/transcripts/format subsystems
## [2026-05-01] update | subsystem/channel-telegram.md — rewrite auth gate (config-only default-deny); sender approval; module split; slash command commands
## [2026-05-01] update | subsystem/channel-discord.md — verify against current code (no content drift)
## [2026-05-01] update | subsystem/channel-voice.md — verify against current code (no content drift)
## [2026-05-01] update | subsystem/channel-jc-events.md — verify against current code (no content drift)
## [2026-05-01] update | subsystem/channel-cron.md — verify against current code (no content drift)
## [2026-05-01] update | subsystem/gateway-queue.md — add email sender policy helper, sender CLI commands, and Company email metrics
## [2026-05-01] update | subsystem/installation-and-cli-routing.md — clarify jc email owns sender policy operations
