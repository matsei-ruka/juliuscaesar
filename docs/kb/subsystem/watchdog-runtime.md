---
title: Watchdog runtime supervision
section: subsystem
status: active
code_anchors:
  - path: bin/jc-watchdog
    symbol: "tick"
  - path: lib/watchdog/watchdog.sh
    symbol: "main_gateway()"
  - path: templates/init-instance/ops/watchdog.conf
    symbol: "CLAUDE_ARGS_EXTRA"
last_verified: 2026-05-01
verified_by: l.mattei
related:
  - contract/config-and-secret-boundaries.md
  - decision/native-cli-over-api-simulation.md
---

## Summary

Watchdog supervises the gateway daemon by default. It is meant to run from cron via `jc-watchdog tick`, keep `jc gateway` alive, and send a Telegram notification when recovery happens.

Legacy live Claude Telegram-plugin supervision remains available with `RUNTIME_MODE=legacy-claude`.

## Components

- `bin/jc-watchdog`: CLI wrapper for tick/install/uninstall/status/test-notify.
- `lib/watchdog/watchdog.sh`: actual supervisor for gateway or legacy Claude mode.
- `<instance>/ops/watchdog.conf`: optional per-instance overrides.
- `/tmp/jc-watchdog-<screen-name>.state`: latest watchdog state.
- `/tmp/jc-watchdog-<screen-name>.log`: supervisor log.

## Important behavior

- Default runtime mode in setup-created instances is `gateway`.
- Default screen name is `jc-<instance-basename>` and is still used for watchdog state/log file names and legacy Claude screens.
- In gateway mode, watchdog checks `<instance>/state/gateway/jc-gateway.pid` and restarts with `jc-gateway --instance-dir <instance> start`.
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
