#!/usr/bin/env bash
# release_update=2026.06.04.1
#
# watchdog: TELEGRAM_PLUGIN_ENABLED opt-out for gateway-mode instances.
#
# TELEGRAM_BOT_TOKEN in .env caused watchdog crash-loop on instances that
# use jc-gateway for Telegram and don't run the bun plugin. Set
# TELEGRAM_PLUGIN_ENABLED=0 in ops/watchdog.conf to disable the check.
#
# No schema, no config migration required.
set -euo pipefail

echo "release_update=2026.06.04.1 watchdog-plugin-check-opt-out"
echo "  watchdog: TELEGRAM_PLUGIN_ENABLED=0 in ops/watchdog.conf disables bun check"
echo "  no instance migration required"
