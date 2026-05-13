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
## [2026-05-01] update | subsystem/installation-and-cli-routing.md — add completion command and guided brain/Telegram setup flow
## [2026-05-01] update | contract/config-and-secret-boundaries.md — clarify setup secret prompt behavior
## [2026-05-01] update | subsystem/memory-system.md — accept active memory state and note derived DB schema reset
## [2026-05-02] update | contract/instance-layout-and-resolution.md — document instance-owned skills path
## [2026-05-06] update | contract/adapter-and-delivery-contracts.md + subsystem/heartbeat-runner.md — structured brain-output parser recovery and sentinel suppression
## [2026-05-06] update | contract/adapter-and-delivery-contracts.md + subsystem/workers-background-agents.md + subsystem/channel-jc-events.md — canonical Telegram push markers suppress framework follow-up delivery
## [2026-05-06] update | contract/adapter-and-delivery-contracts.md — adapters prepend user-local CLI paths for daemon launches
## [2026-05-06] update | contract/instance-layout-and-resolution.md — documented pre-shipped instance skills and skills/Index.md
## [2026-05-06] update | contract/config-and-secret-boundaries.md — documented pre-shipped web/data skill credentials
## [2026-05-06] update | subsystem/installation-and-cli-routing.md + contract/config-and-secret-boundaries.md + contract/instance-layout-and-resolution.md — added jc skills command, provider config writes, tests, and sync behavior
## [2026-05-07] update | subsystem/gateway-queue.md + decision/why-unified-gateway.md + contract/brain-capabilities.md - documented slim triage output and config-owned class-to-brain routing
## [2026-05-07] update | subsystem/gateway-queue.md + contract/config-and-secret-boundaries.md + decision/native-cli-over-api-simulation.md - documented api_classifier triage protocols and direct-provider config
## [2026-05-07] update | subsystem/gateway-queue.md + contract/config-and-secret-boundaries.md + contract/adapter-and-delivery-contracts.md - documented opt-in gateway reply footer boundaries
## [2026-05-08] update | subsystem/voice-dashscope.md + subsystem/channel-voice.md + subsystem/channel-telegram.md + subsystem/gateway-queue.md + contract/config-and-secret-boundaries.md + subsystem/installation-and-cli-routing.md — documented instance-owned voice env, voice routing, and 2026.05.07.01 version
## [2026-05-08] update | contract/config-and-secret-boundaries.md — documented reserved runtime-control env filtering for instance .env helpers
## [2026-05-08] update | contract/adapter-and-delivery-contracts.md + subsystem/workers-background-agents.md - documented OpenCode LM Studio failure classification and session capture
## [2026-05-08] update | subsystem/gateway-queue.md - documented email default-external sender policy behavior
## [2026-05-12] update | subsystem/commitments-and-reengagement.md — added deferred actions and re-engagement subsystem entry
## [2026-05-12] update | subsystem/dream-pipeline.md — added dream reflection pipeline entry
## [2026-05-12] update | subsystem/heartbeat-runner.md — recorded commitments/reengage/dream builtins
## [2026-05-12] update | subsystem/gateway-queue.md — recorded re-engagement reset hook
## [2026-05-12] update | contract/instance-layout-and-resolution.md — recorded commitments/dream scaffold
## [2026-05-12] update | subsystem/installation-and-cli-routing.md — recorded commitments/dream router and installer surface
## [2026-05-12] update | subsystem/installation-and-cli-routing.md — document jc update release hooks and remove public migrate-to-0.3 lifecycle surface
## [2026-05-12] update | subsystem/watchdog-runtime.md — documented intelligent watchdog brain health, long-running notices, and fallback switching
## [2026-05-12] update | subsystem/gateway-queue.md + decision/why-unified-gateway.md — documented user-visible unsafe triage rejection notices
## [2026-05-12] update | subsystem/watchdog-runtime.md — documented failed-event recovery age cap to avoid stale unanswered-message replay
## [2026-05-12] update | subsystem/installation-and-cli-routing.md — bumped current release hook anchor to 2026.05.12.02 hotfix
## [2026-05-13] update | subsystem/watchdog-runtime.md + subsystem/gateway-queue.md — documented recovery-defer parking, latest-message failed recovery, and triage-gated contextual long-running notices
## [2026-05-13] update | subsystem/installation-and-cli-routing.md — bumped current release hook anchor to 2026.05.13.01 hotfix
## [2026-05-13] update | subsystem/watchdog-runtime.md — removed watchdog LLM triage; long-running detection is observe-only
## [2026-05-13] update | subsystem/watchdog-runtime.md — removed watchdog failed-event replay and brain switching
