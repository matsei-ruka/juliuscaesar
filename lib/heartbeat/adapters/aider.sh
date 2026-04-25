#!/usr/bin/env bash
# Aider CLI adapter (https://aider.chat). Reads prompt from stdin, writes
# response to stdout. Model is $1 (optional). Resume model is conversation-
# history-file based: AIDER_HISTORY_DIR (set by the gateway) holds per-session
# history files; JC_RESUME_SESSION is the file stem.
#
# This adapter is intentionally minimal — aider has many flags. Override via
# AIDER_EXTRA_ARGS if needed.
set -euo pipefail

MODEL="${1:-}"
HISTORY_DIR="${AIDER_HISTORY_DIR:-}"
RESUME="${JC_RESUME_SESSION:-${WORKER_RESUME_SESSION:-}}"
EXTRA="${AIDER_EXTRA_ARGS:-}"

if ! command -v aider >/dev/null 2>&1; then
    echo "aider CLI not installed. See https://aider.chat/docs/install.html" >&2
    exit 127
fi

PROMPT_FILE="$(mktemp -t jc-aider.XXXXXX)"
trap 'rm -f "$PROMPT_FILE"' EXIT
cat - > "$PROMPT_FILE"

ARGS=("--yes-always" "--no-pretty" "--no-stream" "--message-file" "$PROMPT_FILE")

if [[ -n "$MODEL" ]]; then
    ARGS+=("--model" "$MODEL")
fi

if [[ -n "$HISTORY_DIR" ]]; then
    mkdir -p "$HISTORY_DIR"
    if [[ -n "$RESUME" ]]; then
        ARGS+=("--input-history-file" "$HISTORY_DIR/$RESUME.input.history")
        ARGS+=("--chat-history-file" "$HISTORY_DIR/$RESUME.chat.history.md")
    else
        ARGS+=("--input-history-file" "$HISTORY_DIR/default.input.history")
        ARGS+=("--chat-history-file" "$HISTORY_DIR/default.chat.history.md")
    fi
fi

# shellcheck disable=SC2086
exec aider $EXTRA "${ARGS[@]}"
