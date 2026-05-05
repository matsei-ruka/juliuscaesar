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

# Structured brain-output contract: stdout MUST be a single JSON object.
# The gateway parses this object and decides whether to deliver to channel.
ARGS+=("--append-system-prompt" "GATEWAY OUTPUT CONTRACT: Your final stdout MUST be a single JSON object on a single line (no code fences, no prose before or after) with exactly these fields:
  {\"push_message_sent\": <bool>, \"message\": <string>}

Rules:
- If you used PushNotification during this task to deliver the user-facing output yourself, set push_message_sent=true. The 'message' field then becomes an audit log of what you pushed (the framework will NOT re-deliver it).
- If you did NOT use PushNotification and want the framework to relay your reply, set push_message_sent=false and put the full reply in 'message'. The framework will deliver it to the channel.
- 'message' is always required (use empty string only for genuine no-op silent runs).
- Emit ONLY the JSON object as your final output. No prefix, no suffix, no explanation, no code fence.")

exec claude "${ARGS[@]}"
