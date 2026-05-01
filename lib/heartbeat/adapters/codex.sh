#!/usr/bin/env bash
# OpenAI Codex CLI adapter (ChatGPT subscription auth or OPENAI_API_KEY).
# Reads prompt from stdin, writes response to stdout. Model is $1 (optional).
#
# Uses `codex exec -` for non-interactive mode: stdin becomes the full prompt,
# final agent message goes to stdout, streaming progress to stderr. Each run
# is a fresh conversation — DOES NOT resume any prior session.
#
# Sandbox mode is controlled by $CODEX_SANDBOX:
#   read-only        (default) — codex may read but not write
#   workspace-write            — codex may edit files inside the working dir
#   yolo                       — bypass all approvals + sandbox (DANGEROUS)
set -euo pipefail

MODEL="${1:-}"
shift || true
SANDBOX="${CODEX_SANDBOX:-read-only}"

if ! command -v codex >/dev/null 2>&1; then
    echo "codex CLI not installed. See https://developers.openai.com/codex/cli" >&2
    exit 127
fi

# `codex exec` and `codex exec resume` differ on sandbox flags:
#   exec        accepts -s/--sandbox <mode>
#   exec resume does NOT — sandbox must be set via -c sandbox_mode=<mode>
# Both accept --dangerously-bypass-approvals-and-sandbox.
RESUME="${JC_RESUME_SESSION:-${WORKER_RESUME_SESSION:-}}"

if [[ -n "$RESUME" ]]; then
    ARGS=("exec" "resume" "$RESUME")
    case "$SANDBOX" in
        read-only|workspace-write)
            ARGS+=("-c" "sandbox_mode=$SANDBOX")
            ;;
        yolo|danger|danger-full-access)
            ARGS+=("--dangerously-bypass-approvals-and-sandbox")
            ;;
        *)
            echo "codex: unknown CODEX_SANDBOX value '$SANDBOX' (use: workspace-write|read-only|yolo)" >&2
            exit 2
            ;;
    esac
else
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
fi

if [[ -n "$MODEL" ]]; then
    ARGS+=("--model" "$MODEL")
fi

if [[ $# -gt 0 ]]; then
    ARGS+=("$@")
fi

# Prompt comes from stdin via the `-` positional.
ARGS+=("-")

exec codex "${ARGS[@]}"
