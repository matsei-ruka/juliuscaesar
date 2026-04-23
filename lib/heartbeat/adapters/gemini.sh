#!/usr/bin/env bash
# Gemini CLI adapter (subscription auth via `gemini auth login`).
# Reads prompt from stdin, writes response to stdout. Model is $1 (optional).
set -euo pipefail

MODEL="${1:-}"

if ! command -v gemini >/dev/null 2>&1; then
    echo "gemini CLI not installed. See https://github.com/google-gemini/gemini-cli" >&2
    exit 127
fi

# gemini CLI: -p/--prompt reads from arg; --model overrides default.
# We capture stdin → arg to keep the adapter stdin-based.
PROMPT=$(cat)
ARGS=("-p" "$PROMPT")
if [[ -n "$MODEL" ]]; then
    ARGS+=("--model" "$MODEL")
fi

exec gemini "${ARGS[@]}"
