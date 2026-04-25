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

# Cron has a minimal PATH. Use $HOME (respects root's /root, not /home/root)
# and fall back to standard system paths.
export PATH="${HOME:-/tmp}/.local/bin:${HOME:-/tmp}/.npm-global/bin:${HOME:-/tmp}/.bun/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

# --- Config defaults ---
SESSION_ID=""
SCREEN_NAME="jc-$(basename "$INSTANCE_DIR")"
CLAUDE_ARGS_EXTRA="--dangerously-skip-permissions --chrome --channels plugin:telegram@claude-plugins-official"

# Source instance config (overrides defaults)
if [[ -f "$CONF_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$CONF_FILE"
fi

STATE_FILE="/tmp/jc-watchdog-${SCREEN_NAME}.state"
LOG_FILE="/tmp/jc-watchdog-${SCREEN_NAME}.log"

load_env_file() {
    local file="$1"
    local line key value
    [[ -f "$file" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# ]] && continue
        [[ "$line" == *"="* ]] || continue
        key="${line%%=*}"
        value="${line#*=}"
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        case "$key" in
            TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID) ;;
            *) continue ;;
        esac
        value="${value#"${value%%[![:space:]]*}"}"
        if [[ "$value" == \'* && "$value" == *\' ]]; then
            value="${value:1:${#value}-2}"
        elif [[ "$value" == \"* && "$value" == *\" ]]; then
            value="${value:1:${#value}-2}"
        fi
        export "$key=$value"
    done < "$file"
}

# Load instance .env data without sourcing shell code from the file.
load_env_file "$ENV_FILE"

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

proc_cwd() {
    local pid="$1"
    local cwd
    cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)
    if [[ -n "$cwd" ]]; then
        echo "$cwd"
        return 0
    fi
    if command -v lsof >/dev/null 2>&1; then
        lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | awk '/^n/ { print substr($0, 2); exit }'
    fi
}

claude_alive() {
    # Scope to THIS instance only: an interactive claude process whose
    # working directory is INSTANCE_DIR. Previously matched any claude
    # with --channels, which falsely identifies unrelated sessions on
    # multi-instance hosts as ours — breaks both detection and restart.
    local pid cwd
    for pid in $(pgrep -u "$(id -un)" -f "claude .*--channels plugin:telegram" 2>/dev/null); do
        cwd=$(proc_cwd "$pid")
        if [[ "$cwd" == "$INSTANCE_DIR" ]]; then
            return 0
        fi
    done
    return 1
}

our_claude_pids() {
    # Emit PIDs of claude processes scoped to this instance (one per line).
    local pid cwd
    for pid in $(pgrep -u "$(id -un)" -f "claude .*--channels plugin:telegram" 2>/dev/null); do
        cwd=$(proc_cwd "$pid")
        if [[ "$cwd" == "$INSTANCE_DIR" ]]; then
            echo "$pid"
        fi
    done
}

# --- Channel plugin liveness -------------------------------------------------
#
# Claude Code's telegram plugin spawns a `bun server.ts` subprocess and writes
# its PID to ~/.claude/channels/telegram/bot.pid. The plugin dies occasionally
# under heavy subprocess load (claude -p spawns, pip installs, git pushes).
# When it dies, inbound telegram messages silently queue at the API and
# outbound tools fail. It can also outlive a crashed claude process as an
# orphan, keeping the bot long-poll and causing the next claude plugin startup
# to fail with Telegram 409 conflicts. The watchdog handles both asymmetric
# states before restart.
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

telegram_plugin_pid() {
    local pidfile="$HOME/.claude/channels/telegram/bot.pid"
    [[ -f "$pidfile" ]] || return 1
    cat "$pidfile" 2>/dev/null
}

kill_telegram_plugin() {
    local pidfile="$HOME/.claude/channels/telegram/bot.pid"
    local pid
    pid=$(telegram_plugin_pid || true)
    if [[ -n "$pid" ]]; then
        kill "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
}

start_julius() {
    if [[ -z "$CLAUDE_BIN" ]]; then
        log "start: no claude binary found on PATH or fallbacks"
        return 1
    fi
    log "Starting screen '$SCREEN_NAME' with claude ($CLAUDE_BIN)..."
    # Pass the instance path and binary as positional args so paths containing
    # quotes or spaces cannot break out of the shell command.
    screen -dmS "$SCREEN_NAME" bash -c 'cd "$1" && shift && exec "$@"' _ "$INSTANCE_DIR" "$CLAUDE_BIN" $CLAUDE_ARGS
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        claude_alive && return 0
        sleep 1
    done
    return 0
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

    # Opposite degraded state: claude crashed, but its telegram plugin bun
    # subprocess was reparented and kept polling the bot token. If we start a
    # new claude while that orphan still owns the long-poll, plugin init can
    # fail with Telegram 409 Conflict and the watchdog loops forever.
    if telegram_plugin_expected && telegram_plugin_alive && ! claude_alive; then
        log "killing orphan telegram plugin before restart"
        kill_telegram_plugin
        sleep 1
    fi

    # Kill this instance's claude if it's still running but degraded (so the
    # restart below is clean; --resume from a new spawn would fight a stale
    # process over session locks). Only kills OUR pids — other instances'
    # claudes on the same host are untouched.
    if claude_alive; then
        log "killing stale claude process(es) to allow clean restart"
        for pid in $(our_claude_pids); do
            kill "$pid" 2>/dev/null || true
        done
        # Wait up to 15s for graceful shutdown.
        for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
            claude_alive || break
            sleep 1
        done
        if claude_alive; then
            log "SIGTERM didn't land — SIGKILL"
            for pid in $(our_claude_pids); do
                kill -9 "$pid" 2>/dev/null || true
            done
            sleep 2
        fi
    fi

    screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
    sleep 1

    if ! start_julius; then
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
