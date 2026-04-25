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
    "--mcp-config" '{"mcpServers":{}}'
    "--strict-mcp-config"
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

exec claude "${ARGS[@]}"
