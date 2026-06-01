#!/usr/bin/env bash
# release_update=2026.06.01.1
#
# Ops hardening + visibility cut. Ships with:
#   - PR #74: heartbeat cron auto-sync + post-update feature audit notifier
#   - PR #75: doctor PID-liveness + watchdog self-install + gateway env-isolation
#
# The 2026.06.01.sh hook (from PR #74) already pings the operator about
# newly-available opt-in features. This script chains a couple of one-shot
# self-heal actions so a fresh upgrade doesn't have to remember them.
set -euo pipefail

INSTANCE_DIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --instance-dir)
            INSTANCE_DIR="${2:-}"
            shift 2
            ;;
        --instance-dir=*)
            INSTANCE_DIR="${1#--instance-dir=}"
            shift
            ;;
        *)
            shift
            ;;
    esac
done

echo "release_update=2026.06.01.1 ops-hardening"

if [[ -z "$INSTANCE_DIR" ]]; then
    echo "  (no --instance-dir; skipping self-heal)"
    exit 0
fi

if ! command -v jc >/dev/null 2>&1; then
    echo "  (jc not on PATH; skipping self-heal)"
    exit 0
fi

# Watchdog self-install: write a JC-WATCHDOG marker block into the calling
# user's crontab if missing. Idempotent. Replaces legacy hand-written entries
# silently so the marker convention takes over. Best-effort.
if jc watchdog install --instance-dir "$INSTANCE_DIR" >/dev/null 2>&1; then
    echo "  watchdog: crontab block installed / refreshed"
else
    echo "  watchdog: install skipped (jc watchdog not available, or crontab unwritable)"
fi

# Heartbeat cron sync: same idea — if the operator has scheduled tasks in
# tasks.yaml, install matching cron lines. Skips silently when none.
if jc heartbeat cron sync --instance-dir "$INSTANCE_DIR" >/dev/null 2>&1; then
    echo "  heartbeat: crontab block synced from tasks.yaml"
else
    echo "  heartbeat: sync skipped"
fi
