#!/usr/bin/env bash
# OpenAI Codex CLI adapter (ChatGPT subscription auth or OPENAI_API_KEY).
# Reads prompt from stdin, writes response to stdout. Model is $1 (optional).
#
# Uses `codex exec -` for non-interactive mode: stdin becomes the full prompt,
# final agent message goes to stdout, streaming progress to stderr. Each run
# is a fresh conversation — DOES NOT resume any prior session.
#
# Sandbox mode is controlled by $CODEX_SANDBOX:
#   workspace-write  (default) — codex may edit files inside the working dir
#   read-only                  — codex may read but not write
#   yolo                       — bypass all approvals + sandbox (DANGEROUS)
set -euo pipefail

MODEL="${1:-}"
SANDBOX="${CODEX_SANDBOX:-workspace-write}"

if ! command -v codex >/dev/null 2>&1; then
    echo "codex CLI not installed. See https://developers.openai.com/codex/cli" >&2
    exit 127
fi

ARGS=("exec")
case "$SANDBOX" in
    read-only|workspace-write)
        ARGS+=("--sandbox" "$SANDBOX")
        ;;
    yolo|danger|danger-full-access)
        ARGS+=("--dangerously-bypass-approvals-and-sandbox")
        ;;
    *)
        echo "codex: unknown CODEX_SANDBOX value '$SANDBOX' (use: workspace-write|read-only|yolo)" >&2
        exit 2
        ;;
esac

ARGS+=("-")

if [[ -n "$MODEL" ]]; then
    ARGS+=("--model" "$MODEL")
fi

exec codex "${ARGS[@]}"
