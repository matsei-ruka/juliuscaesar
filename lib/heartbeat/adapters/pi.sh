#!/usr/bin/env bash
# pi.dev adapter.
# Reads prompt from stdin, writes response to stdout. Model is $1 (optional).
#
# Non-interactive mode: `pi -p` (reads stdin when no positional arg given).
# Resume: set JC_RESUME_SESSION or WORKER_RESUME_SESSION to a session UUID →
#   passes --session <uuid>.
# Tools: JC_PI_NO_TOOLS=1 (default) → --no-tools. Set to 0 to enable tools.
#
# pi -p without a positional argument reads the full prompt from stdin.
# No ARG_MAX limit. No --api-key on the command line (env vars only).
set -euo pipefail

export PATH="${HOME:-/tmp}/.local/bin:${HOME:-/tmp}/.npm-global/bin:${HOME:-/tmp}/.bun/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

MODEL="${1:-}"
shift || true

if ! command -v pi >/dev/null 2>&1; then
    echo "pi CLI not installed. See https://pi.dev" >&2
    exit 127
fi

# Resolve JC model aliases to fully-qualified provider/model IDs.
# Strip "pi:" prefix first if present (worker path may pass "pi:sonnet").
MODEL="${MODEL#pi:}"
case "$MODEL" in
    sonnet|sonnet-4-6)           MODEL="anthropic/claude-sonnet-4-6" ;;
    opus|opus-4-7|opus-4-7-1m)  MODEL="anthropic/claude-opus-4-7"   ;;
    haiku|haiku-4-5*)            MODEL="anthropic/claude-haiku-4-5"  ;;
    gpt-5.4|gpt5|gpt54)         MODEL="openai/gpt-5.4"             ;;
    gpt-5.4-mini|mini)          MODEL="openai/gpt-5.4-mini"        ;;
    gpt-5.3-codex)              MODEL="openai/gpt-5.3-codex"       ;;
    gemini-2.5-pro|gemini25)    MODEL="google/gemini-2.5-pro"      ;;
    gemini-2.0-flash|gemini20)  MODEL="google/gemini-2.0-flash"    ;;
    "")                         ;;  # use pi's default model from settings.json
    *)                          ;;  # pass through as-is (provider/model already)
esac

ARGS=(
    "-p"
    "--no-context-files"
    "--no-extensions"
    "--no-skills"
    "--no-prompt-templates"
    "--no-themes"
)

# Tools: --no-tools is default for gateway chat.
# Allow override via JC_PI_NO_TOOLS=0 for workers/coding tasks.
if [[ "${JC_PI_NO_TOOLS:-1}" != "0" ]]; then
    ARGS+=("--no-tools")
fi

RESUME="${JC_RESUME_SESSION:-${WORKER_RESUME_SESSION:-}}"
if [[ -n "$RESUME" ]]; then
    ARGS+=("--session" "$RESUME")
fi

if [[ -n "$MODEL" ]]; then
    ARGS+=("--model" "$MODEL")
fi

# Extra args from brain config (extra_args_for_event + override.extra_args).
if [[ $# -gt 0 ]]; then
    ARGS+=("$@")
fi

exec pi "${ARGS[@]}"
