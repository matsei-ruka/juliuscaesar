---
title: Adapter and delivery contracts
section: contract
status: active
code_anchors:
  - path: lib/gateway/brain_output.py
    symbol: "def parse_brain_output"
  - path: lib/heartbeat/runner.py
    symbol: "def call_adapter("
  - path: lib/heartbeat/lib/send_telegram.sh
    symbol: "TELEGRAM_CHAT_ID_OVERRIDE"
  - path: lib/heartbeat/lib/send_telegram.py
    symbol: "def _write_push_marker"
  - path: lib/heartbeat/adapters/claude.sh
    symbol: "exec claude"
  - path: lib/heartbeat/adapters/codex.sh
    symbol: "codex exec"
  - path: lib/gateway/brains/dispatch.py
    symbol: "_BRAIN_REGISTRY"
last_verified: 2026-05-07
verified_by: Matsei Ruka
related:
  - subsystem/heartbeat-runner.md
  - subsystem/workers-background-agents.md
---

## Summary

Adapters are executable shell scripts under `lib/heartbeat/adapters/`. Both heartbeat and workers call them with the same contract: model is `$1`, prompt comes on stdin, final answer is stdout, and stderr is diagnostics.

The gateway uses Python wrappers under `lib/gateway/brains/` (one per brain). They share the shell-adapter contract above for subprocess brains (`claude`, `codex`, `gemini`, `opencode`, `aider`). The `codex_api` wrapper is the lone exception — it calls the OpenAI Responses API directly using the local Codex CLI's OAuth token rather than launching a subprocess.

Delivery for heartbeat still uses `lib/heartbeat/lib/send_telegram.sh`. Gateway delivery uses direct channel clients for Telegram, Slack Socket Mode, and Discord. Both gateway and heartbeat parse adapter stdout through `parse_brain_output` before delivery.

Gateway may append the optional `reply_footer` diagnostic line after parsing
and before normal channel delivery. That footer is not part of the adapter
stdout contract and is skipped when the parsed output or push marker says the
brain already delivered its own message.

## Adapter contract

- Path: `lib/heartbeat/adapters/<tool>.sh`
- Executable bit must be set.
- Adapters prepend common user CLI locations (`~/.local/bin`,
  `~/.npm-global/bin`, `~/.bun/bin`) to `PATH` so daemon-launched brains can
  resolve subscription CLIs installed under the agent account.
- Args: `$1` is optional model id.
- Input: stdin is the full prompt.
- Output: stdout is either legacy plain text or the structured gateway envelope
  `{"push_message_sent": <bool>, "message": <string>}`.
- Error: non-zero exit is failure; stderr is captured into logs.
- Resume: `JC_RESUME_SESSION` is the shared env var for gateway/workers; adapters also accept the older `WORKER_RESUME_SESSION` fallback.

## Brain output contract

Migrated brains should emit one JSON envelope on final stdout:

```json
{"push_message_sent": false, "message": "text to relay"}
```

- `push_message_sent=true`: the brain already delivered through PushNotification
  or an equivalent channel push; the framework must not deliver again.
- `push_message_sent=false`: the framework relays `message`.
- Empty `message` is a silent no-op.

The parser is defensive: if a brain emits prose plus a valid envelope, it
consumes the envelope and never relays the surrounding prose or the JSON itself.
Legacy plain text is still relayed. Exact silent sentinels (`SILENT`, `SILENCE`,
`[no-reply]`, etc.) suppress delivery, and cron / jc-events also suppress when
the last non-empty line is one of those sentinels.

When an adapter subprocess uses the canonical `send_telegram` helper directly,
the helper writes `JC_PUSH_MARKER_PATH`; gateway, heartbeat, and worker runners
use that marker to suppress their own follow-up delivery even if the brain
forgot to set `push_message_sent=true`.

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
- Gateway and heartbeat delivery must never relay the structured JSON envelope
  itself to user channels.
- Gateway reply footers are delivery presentation only; adapters should never
  emit them themselves.
- Heartbeat destinations are Telegram-only in 0.1.x.
- Workers share adapter behavior with heartbeat but manage their own lifecycle state; in gateway-configured instances their terminal notifications enqueue delivery events.

## Open questions / known stale

- 2026-04-25: Telegram and Slack Socket Mode are implemented in gateway; ~~Discord~~ and public webhook channels are roadmap work.
- 2026-05-01: Discord shipped (`lib/gateway/channels/discord.py`). Public webhook channel still open.
- 2026-05-01: `JC_IN_WORKER=1` is set when workers spawn an adapter subprocess. Brains that themselves call `jc workers spawn` use this to refuse recursion.
