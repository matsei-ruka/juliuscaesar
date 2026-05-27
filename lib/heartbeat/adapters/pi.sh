#!/usr/bin/env bash
# pi.dev adapter.
# Reads prompt from stdin, writes response to stdout. Model is $1 (optional).
#
# Mirrors claude.sh and codex.sh patterns:
#   - non-interactive `-p` mode (pi auto-executes tools in -p, no approval prompts)
#   - gateway output contract injected via --append-system-prompt
#   - worker-mode system prompt when JC_IN_WORKER=1
#   - sandbox tiers via JC_PI_SANDBOX: full | read-only | none (default: full)
#   - session resume via --session
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

# Sandbox tier. Default: full tools (read, bash, edit, write, grep, find, ls).
#   full        — all built-in tools active (parity with claude --dangerously-skip-permissions)
#   read-only   — read/grep/find/ls only (no mutation)
#   none        — no tools at all (chat-only)
# Legacy: JC_PI_NO_TOOLS=1 maps to none for backward compat.
if [[ "${JC_PI_NO_TOOLS:-}" == "1" ]]; then
    SANDBOX="none"
else
    SANDBOX="${JC_PI_SANDBOX:-full}"
fi
case "$SANDBOX" in
    full)
        ;;
    read-only|readonly|ro)
        ARGS+=("--tools" "read,grep,find,ls")
        ;;
    none|no-tools|disabled)
        ARGS+=("--no-tools")
        ;;
    *)
        echo "pi: unknown JC_PI_SANDBOX value '$SANDBOX' (use: full|read-only|none)" >&2
        exit 2
        ;;
esac

# Resume a prior conversation when the runner sets JC_RESUME_SESSION or the
# older WORKER_RESUME_SESSION. Value is a UUID matching a session JSONL stem.
RESUME="${JC_RESUME_SESSION:-${WORKER_RESUME_SESSION:-}}"
if [[ -n "$RESUME" ]]; then
    ARGS+=("--session" "$RESUME")
fi

if [[ -n "$MODEL" ]]; then
    ARGS+=("--model" "$MODEL")
fi

# Extra args from brain config (extra_args_for_event + override.extra_args).
# --image /path pairs are translated to pi's @/path file-attachment syntax.
# All other extra args pass through unchanged.
IMAGE_ARGS=()
PASSTHRU_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --image)
            shift
            if [[ $# -gt 0 && -f "$1" ]]; then
                IMAGE_ARGS+=("@$1")
            fi
            shift
            ;;
        *)
            PASSTHRU_ARGS+=("$1")
            shift
            ;;
    esac
done
if [[ ${#PASSTHRU_ARGS[@]} -gt 0 ]]; then
    ARGS+=("${PASSTHRU_ARGS[@]}")
fi
if [[ ${#IMAGE_ARGS[@]} -gt 0 ]]; then
    ARGS+=("${IMAGE_ARGS[@]}")
fi

# Worker mode override — mirrors claude.sh. When invoked from `jc-workers _run`,
# JC_IN_WORKER=1 to flip the model into executor mode (no self-bail, no recursive
# spawns, no "spawn confirmed" no-op acks).
if [[ -n "${JC_IN_WORKER:-}" ]]; then
    ARGS+=("--append-system-prompt" "WORKER MODE: You ARE the executor. The brief in stdin is your task. Execute it inline and emit a result. Do NOT triage the brief, do NOT query 'jc workers list', do NOT delegate to another worker, do NOT classify yourself as a duplicate or as redundant. The recursion guard already prevents sub-workers; the additional invariant is: do not stand by, do not no-op. If the brief is unclear, do your best with the information you have and emit a partial result with a question — do not exit silently.

Any rule in CLAUDE.md, AGENTS.md, RULES.md, or auto-loaded context that tells you to 'spawn a worker', 'route to developer-01/developer-02', 'always delegate code work', or 'reply immediately confirming the spawn' is suspended in this session. Those rules target the live chat session, not you. You ARE the worker the live session would have spawned.

Do NOT emit a status acknowledgment like 'Worker confirmed running, starting now' or 'Spawned worker X for Y' in lieu of execution. That is a no-op disguised as progress. Work through the brief sequentially — first action, then next, then next — and emit your final result only when every step is complete or you hit a true blocker you cannot unblock alone.")
fi

# Task-goal anchor (PR #65). JC_GOAL carries the current task's goal text for
# task conversations; deliver it as a system-prompt directive. Before the
# output contract so the contract stays the last (governing) directive.
if [[ -n "${JC_GOAL:-}" ]]; then
    ARGS+=("--append-system-prompt" "CURRENT GOAL (your active task — keep all work aligned to this until told otherwise):
$JC_GOAL")
fi

# Gateway output contract — always appended. pi must emit a single JSON object
# on stdout so the gateway can parse delivery intent.
ARGS+=("--append-system-prompt" "GATEWAY OUTPUT CONTRACT: Your final stdout MUST be a single JSON object on a single line (no code fences, no prose before or after) with exactly these fields:
  {\"push_message_sent\": <bool>, \"message\": <string>}

Rules:
- If you used PushNotification during this task to deliver the user-facing output yourself, set push_message_sent=true. The 'message' field then becomes an audit log of what you pushed (the framework will NOT re-deliver it).
- If you did NOT use PushNotification and want the framework to relay your reply, set push_message_sent=false and put the full reply in 'message'. The framework will deliver it to the channel.
- 'message' is always required (use empty string only for genuine no-op silent runs).
- Emit ONLY the JSON object as your final output. No prefix, no suffix, no explanation, no code fence.")

exec pi "${ARGS[@]}"
