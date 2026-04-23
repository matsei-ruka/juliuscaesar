#!/usr/bin/env bash
# JuliusCaesar watchdog — cron-triggered supervisor for an instance's
# live claude session.
#
# Called as:
#   watchdog.sh <instance_dir>
#
# Reads <instance>/ops/watchdog.conf for per-instance overrides:
#   SESSION_ID        → passed as --resume <id>
#   SCREEN_NAME       → screen session name (default: jc-<instance-basename>)
#   CLAUDE_ARGS_EXTRA → extra args (default: --dangerously-skip-permissions
#                       --chrome --channels plugin:telegram@claude-plugins-official)
#
# Reads <instance>/.env for:
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional — if missing, notifications
#   are skipped but supervision still runs).
#
# State files: /tmp/jc-watchdog-<screen-name>.{state,log}

set -uo pipefail

INSTANCE_DIR="${1:-}"
if [[ -z "$INSTANCE_DIR" || ! -d "$INSTANCE_DIR" ]]; then
    echo "usage: watchdog.sh <instance_dir>" >&2
    exit 2
fi

INSTANCE_DIR="$(cd "$INSTANCE_DIR" && pwd)"
CONF_FILE="$INSTANCE_DIR/ops/watchdog.conf"
ENV_FILE="$INSTANCE_DIR/.env"

# Cron has a minimal PATH — export a useful one BEFORE resolving binaries.
export PATH="/home/$(id -un)/.local/bin:/home/$(id -un)/.npm-global/bin:/home/$(id -un)/.bun/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

# --- Config defaults ---
SESSION_ID=""
SCREEN_NAME="jc-$(basename "$INSTANCE_DIR")"
CLAUDE_ARGS_EXTRA="--dangerously-skip-permissions --chrome --channels plugin:telegram@claude-plugins-official"

# Source instance config (overrides defaults)
if [[ -f "$CONF_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$CONF_FILE"
fi

# Source instance .env (telegram creds)
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
fi

STATE_FILE="/tmp/jc-watchdog-${SCREEN_NAME}.state"
LOG_FILE="/tmp/jc-watchdog-${SCREEN_NAME}.log"

# --- Claude binary resolution ---
CLAUDE_BIN=""
for candidate in "$(command -v claude 2>/dev/null)" \
                 "$HOME/.local/bin/claude" \
                 "$HOME/.npm-global/bin/claude" \
                 "$HOME/.bun/bin/claude"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
        CLAUDE_BIN="$candidate"
        break
    fi
done

# --- Compose claude args ---
CLAUDE_ARGS="$CLAUDE_ARGS_EXTRA"
if [[ -n "${SESSION_ID:-}" ]]; then
    CLAUDE_ARGS="$CLAUDE_ARGS --resume $SESSION_ID"
fi

# --- Helpers ---
log() {
    mkdir -p "$(dirname "$LOG_FILE")"
    printf '[%s] %s\n' "$(date +'%FT%T%z')" "$*" >>"$LOG_FILE"
}

notify() {
    local msg="$1"
    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
        log "notify: telegram creds missing in $ENV_FILE, suppressing notification: $msg"
        return 1
    fi
    curl -sS --max-time 10 -X POST \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${msg}" >>"$LOG_FILE" 2>&1
}

screen_named_alive() {
    screen -ls 2>/dev/null | grep -qE "^[[:space:]]+[0-9]+\.${SCREEN_NAME}([[:space:]]|$)"
}

claude_alive() {
    # Match any claude process that uses our --channels arg (i.e. actual
    # interactive session, not a one-shot `claude -p` spawned by adapters).
    pgrep -u "$(id -un)" -f "claude .*--channels plugin:telegram" >/dev/null 2>&1
}

# --- Channel plugin liveness -------------------------------------------------
#
# Claude Code's telegram plugin spawns a `bun server.ts` subprocess and writes
# its PID to ~/.claude/channels/telegram/bot.pid. The plugin dies occasionally
# under heavy subprocess load (claude -p spawns, pip installs, git pushes).
# When it dies, inbound telegram messages silently queue at the API and
# outbound tools fail. This check detects the dead-plugin-alive-claude case so
# the watchdog restarts claude (which respawns the plugin via --channels).
#
# Only runs when TELEGRAM_BOT_TOKEN is set — instances that don't use telegram
# shouldn't trip this check.

telegram_plugin_expected() {
    [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]
}

telegram_plugin_alive() {
    local pidfile="$HOME/.claude/channels/telegram/bot.pid"
    [[ -f "$pidfile" ]] || return 1
    local pid
    pid=$(cat "$pidfile" 2>/dev/null)
    [[ -n "$pid" ]] || return 1
    kill -0 "$pid" 2>/dev/null
}

start_rachel() {
    if [[ -z "$CLAUDE_BIN" ]]; then
        log "start: no claude binary found on PATH or fallbacks"
        return 1
    fi
    log "Starting screen '$SCREEN_NAME' with claude ($CLAUDE_BIN)..."
    screen -dmS "$SCREEN_NAME" bash -c "cd '$INSTANCE_DIR' && exec '$CLAUDE_BIN' $CLAUDE_ARGS"
    sleep 6
}

read_state() {
    [[ -f "$STATE_FILE" ]] && cat "$STATE_FILE" || echo "unknown"
}

write_state() {
    printf '%s\n' "$1" >"$STATE_FILE"
}

# --- Main ---
main() {
    local prev
    prev=$(read_state)

    if claude_alive; then
        # Degraded state: claude is up but the telegram plugin has died.
        # Messages queue silently at the Telegram API. Force a restart of
        # claude to respawn the plugin via --channels.
        if telegram_plugin_expected && ! telegram_plugin_alive; then
            log "degraded: claude alive but telegram plugin dead — restarting to respawn plugin"
            write_state "plugin-dead"
            # Fall through to the restart path. The main claude process will
            # be killed when screen quit fires below.
        else
            write_state "ok"
            if ! screen_named_alive; then
                log "note: claude running but not in screen '$SCREEN_NAME'. Not restarting (process is alive)."
            fi
            if [[ "$prev" == "down" || "$prev" == "restart-failed" || "$prev" == "plugin-dead" ]]; then
                log "transition: $prev -> ok"
                notify "✨ $SCREEN_NAME back alive"
            fi
            exit 0
        fi
    fi

    log "down: screen_named_alive=$(screen_named_alive && echo y || echo n) claude_alive=$(claude_alive && echo y || echo n) plugin_alive=$(telegram_plugin_alive && echo y || echo n) claude_bin=${CLAUDE_BIN:-<none>}"

    # Kill the main claude process if it's still running but degraded (so the
    # restart below is clean; otherwise --resume from a new spawn would fight
    # the stale process over session locks).
    if claude_alive; then
        log "killing stale claude process to allow clean restart"
        pkill -u "$(id -un)" -f "claude .*--channels plugin:telegram" 2>/dev/null || true
        # Wait up to 15s for it to actually die. If it doesn't, force.
        for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
            claude_alive || break
            sleep 1
        done
        if claude_alive; then
            log "SIGTERM didn't land — SIGKILL"
            pkill -9 -u "$(id -un)" -f "claude .*--channels plugin:telegram" 2>/dev/null || true
            sleep 2
        fi
    fi

    screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
    sleep 1

    if ! start_rachel; then
        write_state "restart-failed"
        if [[ "$prev" == "ok" || "$prev" == "unknown" ]]; then
            notify "⚠️ $SCREEN_NAME watchdog cannot locate the claude binary. Check $LOG_FILE"
        fi
        exit 1
    fi

    if claude_alive; then
        log "restart: success"
        write_state "ok"
        notify "✨ $SCREEN_NAME back alive"
    else
        log "restart: FAILED"
        write_state "restart-failed"
        if [[ "$prev" == "ok" || "$prev" == "unknown" ]]; then
            notify "⚠️ $SCREEN_NAME watchdog tried to restart but claude did not come up. See $LOG_FILE"
        fi
    fi
}

main
