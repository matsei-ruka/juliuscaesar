#!/usr/bin/env bash
# OpenAI Codex CLI adapter (ChatGPT subscription auth or OPENAI_API_KEY).
# Reads prompt from stdin, writes response to stdout. Model is $1 (optional).
#
# Uses `codex exec -` for non-interactive mode: stdin becomes the full prompt,
# final agent message goes to stdout, streaming progress to stderr. Each run
# is a fresh conversation — DOES NOT resume any prior session.
set -euo pipefail

MODEL="${1:-}"

if ! command -v codex >/dev/null 2>&1; then
    echo "codex CLI not installed. See https://developers.openai.com/codex/cli" >&2
    exit 127
fi

ARGS=("exec" "-")
if [[ -n "$MODEL" ]]; then
    ARGS+=("--model" "$MODEL")
fi

exec codex "${ARGS[@]}"
