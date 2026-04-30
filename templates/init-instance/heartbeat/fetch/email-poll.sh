#!/usr/bin/env bash
# heartbeat/fetch/email-poll.sh — poll IMAP and dispatch new email to gateway.
#
# Designed to be invoked from cron, NOT from the heartbeat tasks.yaml LLM
# pipeline. The runner doesn't speak to a model — it fetches new messages
# via lib.channels.email.EmailChannelAdapter, then routes them through
# gateway.channels.email_dispatcher (allowed → enqueue, unknown → notify
# Telegram + persist pending, blocked → drop).
#
# Suggested cron:
#   */5 * * * * /home/<user>/<instance>/heartbeat/fetch/email-poll.sh \
#                   >> /home/<user>/<instance>/state/channels/email/cron.log 2>&1
#
# A serializing flock is used so a slow IMAP fetch never overlaps with the
# next tick. The poller is a no-op if `channels.email.enabled: false` in
# ops/gateway.yaml or the IMAP credentials are missing in .env.

set -euo pipefail

# Resolve instance dir = grandparent of this script (heartbeat/fetch/<here>).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTANCE_DIR="$(cd "$HERE/../.." && pwd)"

# Locate the JuliusCaesar framework lib/. Prefer JC_FRAMEWORK_DIR override
# (used in tests), then standard install locations.
FRAMEWORK_LIB=""
if [[ -n "${JC_FRAMEWORK_DIR:-}" && -d "$JC_FRAMEWORK_DIR/lib" ]]; then
    FRAMEWORK_LIB="$JC_FRAMEWORK_DIR/lib"
fi
if [[ -z "$FRAMEWORK_LIB" ]]; then
    for cand in "$HOME/juliuscaesar/lib" "$HOME/.local/share/juliuscaesar/lib" "/opt/juliuscaesar/lib"; do
        if [[ -d "$cand" ]]; then
            FRAMEWORK_LIB="$cand"
            break
        fi
    done
fi
if [[ -z "$FRAMEWORK_LIB" ]]; then
    echo "email-poll: cannot locate JuliusCaesar lib/ — set JC_FRAMEWORK_DIR" >&2
    exit 2
fi

LOCK_DIR="$INSTANCE_DIR/state/channels/email"
LOCK_FILE="$LOCK_DIR/poll.lock"
LOG_DIR="$LOCK_DIR"
LOG_FILE="$LOG_DIR/cron.log"
mkdir -p "$LOCK_DIR" "$LOG_DIR"

PYTHONPATH_VAL="$FRAMEWORK_LIB${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH="$PYTHONPATH_VAL"
export JC_INSTANCE_DIR="$INSTANCE_DIR"

VERBOSE_FLAG=""
if [[ "${1:-}" == "-v" || "${1:-}" == "--verbose" ]]; then
    VERBOSE_FLAG="-v"
fi

# flock(1) is universally available on Linux; bail loud if missing.
if ! command -v flock >/dev/null 2>&1; then
    echo "email-poll: flock(1) required but not found" >&2
    exit 2
fi

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

run_poll() {
    echo "[$(ts)] email-poll: starting (instance=$INSTANCE_DIR)" >> "$LOG_FILE"
    if python3 -m gateway.channels.email_dispatcher poll \
        --instance-dir "$INSTANCE_DIR" $VERBOSE_FLAG; then
        echo "[$(ts)] email-poll: ok" >> "$LOG_FILE"
    else
        rc=$?
        echo "[$(ts)] email-poll: FAILED rc=$rc" >> "$LOG_FILE"
        return $rc
    fi
}

# Acquire non-blocking lock; if held, the previous tick is still running and
# we exit cleanly so cron doesn't pile up.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(ts)] email-poll: skipped (lock held by pid $(cat "$LOCK_FILE" 2>/dev/null || echo '?'))" >> "$LOG_FILE"
    exit 0
fi
echo "$$" > "$LOCK_FILE"

run_poll
