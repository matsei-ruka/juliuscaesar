#!/usr/bin/env bash
# release_update=2026.06.01
#
# Surfaces newly-available opt-in features over Telegram. First run on each
# instance reports every currently-disabled feature; subsequent runs report
# only what was added since the prior snapshot (state/feature-audit-snapshot.json).
#
# Best-effort: a Telegram outage or an instance without the channel wired up
# must not break the upgrade.
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

echo "release_update=2026.06.01 cron-sync+feature-audit"

if [[ -z "$INSTANCE_DIR" ]]; then
    echo "  (no --instance-dir; skipping feature notify)"
    exit 0
fi

if ! command -v jc >/dev/null 2>&1; then
    echo "  (jc not on PATH; skipping feature notify)"
    exit 0
fi

jc features notify-disabled --only-new --instance-dir "$INSTANCE_DIR" || {
    echo "  (jc features notify-disabled failed; continuing upgrade)"
    exit 0
}
