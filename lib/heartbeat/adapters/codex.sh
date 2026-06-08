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

export PATH="${HOME:-/tmp}/.local/bin:${HOME:-/tmp}/.npm-global/bin:${HOME:-/tmp}/.bun/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

MODEL="${1:-}"
shift || true
SANDBOX="${CODEX_SANDBOX:-workspace-write}"

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
    ARGS=("exec" "resume" "--skip-git-repo-check" "$RESUME")
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
    ARGS=("exec" "--skip-git-repo-check")
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

# Prompt comes from stdin via the `-` positional. We prepend the gateway
# output contract so codex emits structured JSON the framework can parse —
# codex CLI has no --append-system-prompt flag, so the contract rides in
# the prompt body as a hard rule the model reads first.
ARGS+=("-")

{
    cat <<'CONTRACT'
[GATEWAY OUTPUT CONTRACT — read carefully, this overrides any default behavior.]

Your FINAL stdout MUST be a single JSON object on a single line (no code fences,
no prose before or after) with exactly these fields:

  {"push_message_sent": <bool>, "message": <string>}

Rules:
- If you used PushNotification (or any equivalent push tool) to deliver the
  user-facing output yourself during this task, set push_message_sent=true.
  The 'message' field then becomes a short audit log of what you pushed —
  the framework will NOT re-deliver it.
- If you did NOT use PushNotification and want the framework to relay your
  reply to the user, set push_message_sent=false and put the full reply in
  'message'. The framework will deliver it to the channel.
- 'message' is always required. Use an empty string only for genuine no-op
  silent runs.
- Emit ONLY the JSON object as your final output. No prefix, no suffix, no
  explanation, no code fence.

[END GATEWAY OUTPUT CONTRACT]

CONTRACT
    cat
} | codex "${ARGS[@]}"
