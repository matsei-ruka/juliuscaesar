#!/usr/bin/env bash
# Gemini CLI adapter (subscription auth via `gemini auth login`).
# Reads prompt from stdin, writes response to stdout. Model is $1 (optional).
#
# Access mode is controlled by $GEMINI_YOLO:
#   true     (default) — --yolo: auto-approve all tools, no sandbox. Required
#                        for workers that need to read files outside the
#                        instance cwd (e.g. code review touching the framework).
#   plan     — --approval-mode plan: read-only, safe.
#   false    — alias for plan.
#   sandbox  — --sandbox: sandboxed to cwd. Restricts cross-repo reads, so
#                         only pick this when the task is truly self-contained.
set -euo pipefail

MODEL="${1:-}"
YOLO="${GEMINI_YOLO:-true}"

if ! command -v gemini >/dev/null 2>&1; then
    echo "gemini CLI not installed. See https://github.com/google-gemini/gemini-cli" >&2
    exit 127
fi

# gemini CLI: -p/--prompt reads from arg; --model overrides default.
# We capture stdin → arg to keep the adapter stdin-based.
PROMPT=$(cat)
ARGS=("-p" "$PROMPT")

case "$YOLO" in
    true|yolo)
        ARGS+=("--yolo")
        ;;
    plan|false|read-only)
        ARGS+=("--approval-mode" "plan")
        ;;
    sandbox)
        ARGS+=("--sandbox")
        ;;
    *)
        echo "gemini: unknown GEMINI_YOLO value '$YOLO' (use: true|plan|sandbox)" >&2
        exit 2
        ;;
esac

if [[ -n "$MODEL" ]]; then
    ARGS+=("--model" "$MODEL")
fi

exec gemini "${ARGS[@]}"
