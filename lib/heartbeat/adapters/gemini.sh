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

# gemini's -p triggers headless mode. Per `gemini --help`, the prompt string is
# "appended to input on stdin (if any)", so we pass an empty -p and let the real
# prompt flow in via stdin. Passing a large prompt as a CLI arg would hit
# ARG_MAX (~128KB in practice on Linux) and fail with "Argument list too long".
ARGS=("-p" "")

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

# Resume a prior conversation when the runner sets WORKER_RESUME_SESSION.
# Value is the session id (or "latest" / index) accepted by `gemini --resume`.
if [[ -n "${WORKER_RESUME_SESSION:-}" ]]; then
    ARGS+=("--resume" "$WORKER_RESUME_SESSION")
fi

exec gemini "${ARGS[@]}"
