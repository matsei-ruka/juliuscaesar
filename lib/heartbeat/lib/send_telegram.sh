#!/usr/bin/env bash
# Thin wrapper around the canonical Python sender (send_telegram.py).
#
# Reads body from stdin. Prints resulting message_id on success.
# All escaping + MarkdownV2 handling lives in the python helper so every
# sender (heartbeat, worker notifications, watchdog status) renders the
# same as the gateway's TelegramChannel.send.
#
# Kept as a `.sh` for backwards compatibility with downstream callers
# that hard-coded the path (jc-workers `_telegram_direct`, watchdog
# notify(), instance-local cron jobs). New code should call the .py
# directly.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/send_telegram.py"

if [[ ! -x "$PY" ]]; then
    echo "send_telegram: $PY not found or not executable" >&2
    exit 2
fi

exec python3 "$PY" "$@"
