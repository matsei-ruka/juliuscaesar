# Knowledge Base - Index

> **Purpose**: a small, current map of how this project actually works.
> Load only the files you need for the task. The `code_anchors` in each file's
> frontmatter tell you what code the entry depends on; re-grep before acting
> on anything you read here.
>
> **Last rebuilt**: 2026-04-25

## How to use

1. Find your topic in the tables below.
2. Read the matching file in full - they are small (~150 lines).
3. Re-grep the `code_anchors` before acting on what you read.
4. Stale or wrong? Use `/kb update`. Never silently delete - move to `## Open questions / known stale`.

## Topic map

<!-- One table per section defined in kb-config.yaml.
     `/kb new` appends a row to the matching table.
     Remove sections you don't use, and add any custom sections you define. -->

### domain

| Topic | File | What's inside |
|-------|------|---------------|
| JuliusCaesar personal assistant framework | domain/personal-assistant-framework.md | Product shape, two-repo model, and core invariants |

### subsystem

| Topic | File | What's inside |
|-------|------|---------------|
| Installation and CLI routing | subsystem/installation-and-cli-routing.md | Installer shims, venv, top-level `jc` dispatch |
| Layered memory system | subsystem/memory-system.md | L1/L2 markdown memory, FTS5 index, `jc memory` |
| Heartbeat scheduled task runner | subsystem/heartbeat-runner.md | Scheduled task pipeline, prompts, deltas, destinations |
| Watchdog runtime supervision | subsystem/watchdog-runtime.md | Gateway daemon supervision plus legacy Claude plugin fallback |
| On-demand background workers | subsystem/workers-background-agents.md | Worker DB, detached lifecycle, named worker resume |
| DashScope voice subsystem | subsystem/voice-dashscope.md | Voice enrollment, synthesis, transcription |
| Gateway runtime and event queue | subsystem/gateway-queue.md | Telegram/Slack gateway runtime, SQLite queue, dispatch, delivery |
| Discord channel | subsystem/channel-discord.md | discord.py-backed inbound DM/mention, outbound channel/thread reply |
| Voice channel | subsystem/channel-voice.md | Paired-channel ASR/TTS via DashScope |
| jc-events channel | subsystem/channel-jc-events.md | Internal worker/system event ingestion via `state/events/` |
| Cron channel | subsystem/channel-cron.md | Scheduled task → gateway event with pinned brain |

### contract

| Topic | File | What's inside |
|-------|------|---------------|
| Instance layout and resolution contract | contract/instance-layout-and-resolution.md | Instance scaffold, `.jc`, resolution precedence |
| Adapter and delivery contracts | contract/adapter-and-delivery-contracts.md | Brain adapters, resume env, heartbeat and gateway delivery |
| Brain capability matrix | contract/brain-capabilities.md | Per-brain support for tools/vision/edits/web + resume mechanism |
| Config and secret boundaries | contract/config-and-secret-boundaries.md | `.env`, gateway/tasks/watchdog config, doctor diagnostics |

### decision

| Topic | File | What's inside |
|-------|------|---------------|
| Native CLI orchestration instead of API simulation | decision/native-cli-over-api-simulation.md | Why JC shells out to native assistant CLIs |
| Why a unified gateway | decision/why-unified-gateway.md | Pain points 0.2.x → architectural answers in 0.3.0 |

### source

| Topic | File | What's inside |
|-------|------|---------------|
| Project documentation map | source/project-documentation-map.md | README, quickstart, architecture, roadmap, specs map |

## File contract

See the `kb` skill's README for the frontmatter contract and hard rules. In short:

- Required frontmatter: `title`, `section`, `code_anchors`, `last_verified`, `verified_by`.
- Every `code_anchors` entry is verified (Glob + Grep) before being written.
- Wrong content is never silently deleted - it moves to `## Open questions / known stale` with a dated note.
- `last_verified` is bumped only when the code was actually re-read.
