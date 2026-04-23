#!/usr/bin/env bash
# Send a plain-text Telegram message for a JuliusCaesar instance.
#
# Reads body from stdin. Prints resulting message_id on success.
# Token + chat_id are read from the instance's .env, NOT the framework.
#
# Instance resolution (in order):
#   1. $JC_INSTANCE_DIR
#   2. Walk up from cwd for a .jc marker
#   3. cwd if it looks like an instance (memory/ exists)
#
# Exits non-zero on failure; stderr carries a short error note.
set -euo pipefail

resolve_instance_dir() {
    if [[ -n "${JC_INSTANCE_DIR:-}" && -d "$JC_INSTANCE_DIR" ]]; then
        echo "$JC_INSTANCE_DIR"
        return 0
    fi
    local cur
    cur="$(pwd)"
    while :; do
        if [[ -f "$cur/.jc" ]]; then
            echo "$cur"
            return 0
        fi
        local parent
        parent="$(dirname "$cur")"
        [[ "$parent" == "$cur" ]] && break
        cur="$parent"
    done
    if [[ -d "$(pwd)/memory" ]]; then
        echo "$(pwd)"
        return 0
    fi
    return 1
}

INSTANCE_DIR="$(resolve_instance_dir)" || {
    echo "send_telegram: could not resolve instance dir" >&2
    exit 2
}
ENV_FILE="$INSTANCE_DIR/.env"

if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
fi

: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN not set (define in $ENV_FILE)}"

# Precedence for chat_id:
#   1. $TELEGRAM_CHAT_ID_OVERRIDE (used by the runner for named destinations)
#   2. $TELEGRAM_CHAT_ID from the instance's .env
if [[ -n "${TELEGRAM_CHAT_ID_OVERRIDE:-}" ]]; then
    TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID_OVERRIDE"
fi
: "${TELEGRAM_CHAT_ID:?TELEGRAM_CHAT_ID not set (define in $ENV_FILE or pass TELEGRAM_CHAT_ID_OVERRIDE)}"

BODY=$(cat)
if [[ -z "${BODY// }" ]]; then
    echo "send_telegram: empty body, refusing to send" >&2
    exit 2
fi

RESP=$(
    curl -sS --max-time 20 -X POST \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${BODY}" \
        --data-urlencode "disable_web_page_preview=true"
)

OK=$(printf '%s' "$RESP" | python3 -c 'import sys, json; print(json.load(sys.stdin).get("ok"))' 2>/dev/null || echo "false")
if [[ "$OK" != "True" && "$OK" != "true" ]]; then
    echo "send_telegram: API returned error: $RESP" >&2
    exit 1
fi

printf '%s' "$RESP" | python3 -c 'import sys, json; print(json.load(sys.stdin)["result"]["message_id"])'
