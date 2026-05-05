#!/usr/bin/env bash
# Claude Code adapter.
# Reads prompt from stdin, writes response to stdout. Model is $1 (optional).
# Runs `claude -p` in a fresh non-interactive session — DOES NOT touch the main
# interactive session (the one serving Telegram).
set -euo pipefail

MODEL="${1:-}"

# Use subscription auth (no API key env var set). No --channels flag: scheduled
# runs should never bind to the telegram channel.
ARGS=(
    "-p"
    "--dangerously-skip-permissions"
    "--chrome"
)
if [[ -n "$MODEL" ]]; then
    ARGS+=("--model" "$MODEL")
fi

# Resume a prior conversation when the runner sets JC_RESUME_SESSION or the
# older WORKER_RESUME_SESSION.
# Value is a UUID matching ~/.claude/projects/<slug>/<uuid>.jsonl.
RESUME="${JC_RESUME_SESSION:-${WORKER_RESUME_SESSION:-}}"
if [[ -n "$RESUME" ]]; then
    ARGS+=("--resume" "$RESUME")
fi

# For cron/jc-events sources: if the task sent content via PushNotification,
# output exactly SILENT so the gateway skips text delivery (avoiding a second
# duplicate message). If the task did NOT use PushNotification and needs the
# gateway to relay text to Telegram, write the message normally.
SOURCE="${JC_EVENT_SOURCE:-}"
if [[ "$SOURCE" == "cron" ]] || [[ "$SOURCE" == "jc-events" ]]; then
    ARGS+=("--append-system-prompt" "GATEWAY RULE: If you used PushNotification during this task, your final text output MUST be exactly the single word: SILENT — no summary, no confirmation, nothing else. If you did NOT use PushNotification, write your reply normally (it will be relayed to Telegram by the gateway).")
fi

exec claude "${ARGS[@]}"
