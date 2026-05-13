---
title: Watchdog runtime supervision
section: subsystem
status: active
code_anchors:
  - path: bin/jc-watchdog
    symbol: "tick"
  - path: lib/watchdog/watchdog.sh
    symbol: "main_gateway()"
  - path: lib/watchdog/intelligence/runner.py
    symbol: "def run_tick"
  - path: templates/init-instance/ops/watchdog.yaml
    symbol: "watchdog:"
  - path: templates/init-instance/ops/watchdog.conf
    symbol: "CLAUDE_ARGS_EXTRA"
last_verified: 2026-05-13
verified_by: Matsei Ruka
related:
  - contract/config-and-secret-boundaries.md
  - decision/native-cli-over-api-simulation.md
---

## Summary

Watchdog supervises the gateway daemon by default. It is meant to run from cron
via `jc-watchdog tick`, keep `jc gateway` alive, and send notifications for
intelligent brain-health handling when active evidence is clear.

Legacy live Claude Telegram-plugin supervision remains available with `RUNTIME_MODE=legacy-claude`.

## Components

- `bin/jc-watchdog`: CLI wrapper for tick/install/uninstall/status/test-notify,
  plus intelligent watchdog inspection (`intelligence`) and brain cooldown
  reset (`reset-brain`).
- `lib/watchdog/watchdog.sh`: legacy bash supervisor for gateway or legacy
  Claude mode; in gateway mode it also invokes the intelligent watchdog tick
  after confirming the daemon is alive.
- `lib/watchdog/supervisor.py`: v2 child supervisor; calls intelligent
  watchdog when the `jc-gateway` child is healthy.
- `lib/watchdog/intelligence/`: queue/log snapshot, deterministic health
  evaluator, dedupe/cooldown state, direct notifications, and brain-health
  cooldowns.
- `<instance>/ops/watchdog.conf`: optional per-instance overrides.
- `<instance>/ops/watchdog.yaml`: v2 supervisor config and `watchdog:`
  intelligence config block.
- `/tmp/jc-watchdog-<screen-name>.state`: latest watchdog state.
- `/tmp/jc-watchdog-<screen-name>.log`: supervisor log.
- `<instance>/state/watchdog/intelligence.json`: dedupe state for user-visible
  notices and brain health cooldowns.

## Important behavior

- Default runtime mode in setup-created instances is `gateway`.
- Default screen name is `jc-<instance-basename>` and is still used for watchdog state/log file names and legacy Claude screens.
- In gateway mode, watchdog checks `<instance>/state/gateway/jc-gateway.pid` and restarts with `jc-gateway --instance-dir <instance> start`.
- Once the gateway daemon is alive, intelligent watchdog inspects
  `state/gateway/queue.db` and `state/gateway/gateway.log` for running events
  older than `watchdog.long_running_notice_seconds` (default 180s) and recent
  active brain/auth evidence in logs. It does not scan terminal failed events
  for missed-message recovery.
- Long-running user requests are observed only. Watchdog records a
  `long_running` decision for visibility but does not send progress chat
  messages or ask an LLM to invent one.
- Brain/auth failures are evaluated by deterministic heuristics only. Normal
  gateway triage is separate and still handles incoming message routing.
- Watchdog does not switch, retry, replay, or create `auth_pending` for user
  message events. Gateway recovery owns adapter-failure retry/replay.
- Brain cooldowns prevent repeated watchdog notices for a known-bad brain; clear
  them with `jc watchdog reset-brain <brain>`.
- Default Claude args include `--dangerously-skip-permissions --chrome --channels plugin:telegram@claude-plugins-official`.
- `SESSION_ID` from watchdog config adds `--resume <id>`.
- Cron install writes both `@reboot` and `*/2 * * * *` entries tagged with the instance path, with command paths and instance paths shell-quoted for spaces.
- Detection scopes Claude processes by working directory, which matters on multi-instance hosts.
- Telegram plugin health requires the `bot.pid` process to descend from this instance's Claude process. A live pidfile owned by a different Claude is treated as hijacked/orphaned and killed before restart.
- Runtime startup passes the instance path and Claude binary as positional args to `bash -c`, not interpolated shell text.

## Degraded plugin handling

This section applies only in `RUNTIME_MODE=legacy-claude`.

If Telegram credentials exist, watchdog expects `~/.claude/channels/telegram/bot.pid` to exist, point to a live process, and be descended from this instance's Claude process. If Claude is alive but the plugin is dead or the pidfile belongs to a different Claude, watchdog marks `plugin-dead`, kills this instance's Claude process, quits the screen session, and restarts Claude so the channel plugin respawns.

If Claude is dead but the plugin process is still alive, or if a foreign Claude/plugin owns the singleton pidfile, watchdog treats the plugin as an orphan/hijacker. It kills the pid from `bot.pid` plus any direct `bun` launcher parent, removes the stale pidfile, waits briefly, then starts a new Claude session. This prevents Telegram 409 Conflict loops and silent update consumption by the wrong session.

## Open questions / known stale

- 2026-04-25: Gateway mode is the default, but legacy Claude plugin mode remains for compatibility.
