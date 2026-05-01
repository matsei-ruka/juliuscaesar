---
title: Adapter and delivery contracts
section: contract
status: active
code_anchors:
  - path: lib/heartbeat/runner.py
    symbol: "def call_adapter("
  - path: lib/heartbeat/lib/send_telegram.sh
    symbol: "TELEGRAM_CHAT_ID_OVERRIDE"
  - path: lib/heartbeat/adapters/claude.sh
    symbol: "exec claude"
  - path: lib/heartbeat/adapters/codex.sh
    symbol: "codex exec"
  - path: lib/gateway/brains/dispatch.py
    symbol: "_BRAIN_REGISTRY"
last_verified: 2026-05-01
verified_by: l.mattei
related:
  - subsystem/heartbeat-runner.md
  - subsystem/workers-background-agents.md
---

## Summary

Adapters are executable shell scripts under `lib/heartbeat/adapters/`. Both heartbeat and workers call them with the same contract: model is `$1`, prompt comes on stdin, final answer is stdout, and stderr is diagnostics.

The gateway uses Python wrappers under `lib/gateway/brains/` (one per brain). They share the shell-adapter contract above for subprocess brains (`claude`, `codex`, `gemini`, `opencode`, `aider`). The `codex_api` wrapper is the lone exception — it calls the OpenAI Responses API directly using the local Codex CLI's OAuth token rather than launching a subprocess.

Delivery for heartbeat still uses `lib/heartbeat/lib/send_telegram.sh`. Gateway delivery uses direct channel clients for Telegram, Slack Socket Mode, and Discord.

## Adapter contract

- Path: `lib/heartbeat/adapters/<tool>.sh`
- Executable bit must be set.
- Args: `$1` is optional model id.
- Input: stdin is the full prompt.
- Output: stdout is the final result.
- Error: non-zero exit is failure; stderr is captured into logs.
- Resume: `JC_RESUME_SESSION` is the shared env var for gateway/workers; adapters also accept the older `WORKER_RESUME_SESSION` fallback.

## Implemented adapters

- `claude.sh`: runs `claude -p`, subscription-authenticated, no Telegram channel binding.
- `codex.sh`: runs `codex exec` or `codex exec resume`; sandbox controlled by `CODEX_SANDBOX`.
- `gemini.sh`: runs `gemini -p ""`; access mode controlled by `GEMINI_YOLO`.
- `opencode.sh`: runs `opencode run`; truncates very large prompts to avoid argv limits.
- `aider.sh`: runs `aider`; resume via per-id history file under `AIDER_HISTORY_DIR`.
- `minimax.sh`: stub on disk; **not registered** in `_BRAIN_REGISTRY`. Do not advertise as supported.

## Gateway brain wrappers

`lib/gateway/brains/<name>.py` (one per registered brain) wraps the shell adapter and adds: session capture, prompt rendering, MCP context plumbing, and adapter-rc → recovery-classifier mapping. `codex_api.py` is the no-subprocess exception (direct Responses API).

## Telegram delivery contract

`send_telegram.sh` resolves the instance from `JC_INSTANCE_DIR`, walk-up `.jc`, or cwd with `memory/`. It sources `<instance>/.env`, requires `TELEGRAM_BOT_TOKEN`, and picks chat id by:

1. `TELEGRAM_CHAT_ID_OVERRIDE`
2. `TELEGRAM_CHAT_ID`

It refuses empty bodies, disables web previews, and prints the resulting `message_id`.

## Invariants

- Scheduled and gateway `claude -p` runs must not use `--channels`; Telegram/Slack are owned by the gateway runtime.
- Heartbeat destinations are Telegram-only in 0.1.x.
- Workers share adapter behavior with heartbeat but manage their own lifecycle state; in gateway-configured instances their terminal notifications enqueue delivery events.

## Open questions / known stale

- 2026-04-25: Telegram and Slack Socket Mode are implemented in gateway; ~~Discord~~ and public webhook channels are roadmap work.
- 2026-05-01: Discord shipped (`lib/gateway/channels/discord.py`). Public webhook channel still open.
- 2026-05-01: `JC_IN_WORKER=1` is set when workers spawn an adapter subprocess. Brains that themselves call `jc workers spawn` use this to refuse recursion.
