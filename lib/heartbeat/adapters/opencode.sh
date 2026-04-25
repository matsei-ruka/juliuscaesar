#!/usr/bin/env bash
# OpenCode adapter (opencode.ai).
# Reads prompt from stdin, writes response to stdout. Model is $1 (optional).
#
# Non-interactive mode: `opencode run --dangerously-skip-permissions <prompt>`
# Resume: set WORKER_RESUME_SESSION to a session ID → passes --session <id>
#
# Prompt is written to a temp file because `opencode run` takes the message
# as a positional arg (not stdin).
set -euo pipefail

MODEL="${1:-}"

if ! command -v opencode >/dev/null 2>&1; then
    echo "opencode CLI not installed. See https://opencode.ai" >&2
    exit 127
fi

PROMPT=$(cat)

ARGS=("run" "--dangerously-skip-permissions")

if [[ -n "${WORKER_RESUME_SESSION:-}" ]]; then
    ARGS+=("--session" "$WORKER_RESUME_SESSION")
fi

if [[ -n "$MODEL" ]]; then
    ARGS+=("--model" "$MODEL")
fi

ARGS+=("$PROMPT")

exec opencode "${ARGS[@]}"
