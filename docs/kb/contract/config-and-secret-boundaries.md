---
title: Config and secret boundaries
section: contract
status: active
code_anchors:
  - path: README.md
    symbol: "Secrets live in `<instance>/.env`"
  - path: bin/jc-init
    symbol: "chmod 600 \"$TARGET/.env\""
  - path: bin/jc-doctor
    symbol: "TELEGRAM_BOT_TOKEN valid"
  - path: lib/heartbeat/runner.py
    symbol: "load_dotenv(str(env_file))"
last_verified: 2026-04-25
verified_by: l.mattei
related:
  - contract/instance-layout-and-resolution.md
  - subsystem/heartbeat-runner.md
  - subsystem/voice-dashscope.md
  - subsystem/watchdog-runtime.md
---

## Summary

The framework keeps user secrets and identity out of the framework repo. Runtime secrets live in the instance `.env`; instance config files live under instance subdirectories such as `heartbeat/`, `ops/`, and `voice/`.

## Secret files

Primary secret file:

- `<instance>/.env`

Common keys:

- `DASHSCOPE_API_KEY` for voice operations.
- `TELEGRAM_BOT_TOKEN` for Telegram delivery and watchdog notifications.
- `TELEGRAM_CHAT_ID` for default delivery.

`jc init` creates `.env` when missing and applies mode 600.

## Config files

- `<instance>/heartbeat/tasks.yaml`: scheduled task definitions, defaults, destinations.
- `<instance>/ops/watchdog.conf`: live session resume id, screen name, Claude args.
- `<instance>/voice/references/voice.json`: voice id and target model after enrollment.
- `<instance>/CLAUDE.md`: imports L1 memory and describes how sessions load context.
- `<instance>/.codex/`: initial Codex-related config from the template.

## Diagnostics

`jc doctor` validates:

- environment binaries,
- `jc-*` binaries,
- instance structure,
- memory index presence,
- heartbeat task and destination shape,
- voice enrollment presence,
- `.env` existence and selected credentials,
- watchdog cron state,
- live Claude and Telegram plugin health,
- worker state directory and DB.

## Invariants

- Framework code should not contain instance credentials.
- Telegram `getMe` validation is only possible when a token is present.
- Voice calls fail fast when `DASHSCOPE_API_KEY` is missing.
- `watchdog.conf` is sourced by bash, so it must stay shell-compatible.

## Open questions / known stale

- 2026-04-25: Roadmap lists a config schema validator as future work.
