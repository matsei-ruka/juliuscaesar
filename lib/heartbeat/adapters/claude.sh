#!/usr/bin/env bash
# Claude Code adapter.
# Reads prompt from stdin, writes response to stdout. Model is $1 (optional).
# Runs `claude -p` in a fresh non-interactive session — DOES NOT touch the main
# interactive session (the one serving Telegram).
set -euo pipefail

export PATH="${HOME:-/tmp}/.local/bin:${HOME:-/tmp}/.npm-global/bin:${HOME:-/tmp}/.bun/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

MODEL="${1:-}"

# Use subscription auth (no API key env var set). No --channels flag: scheduled
# runs should never bind to the telegram channel.
ARGS=(
    "-p"
    "--dangerously-skip-permissions"
    "--chrome"
)
if [[ -n "$MODEL" ]]; then
    ARGS+=("--model" "$MODEL")
fi

# Resume a prior conversation when the runner sets JC_RESUME_SESSION or the
# older WORKER_RESUME_SESSION.
# Value is a UUID matching ~/.claude/projects/<slug>/<uuid>.jsonl.
RESUME="${JC_RESUME_SESSION:-${WORKER_RESUME_SESSION:-}}"
if [[ -n "$RESUME" ]]; then
    ARGS+=("--resume" "$RESUME")
fi

# Worker mode-flip: when JC_IN_WORKER=1 the brain runs as a background worker
# with cwd=instance_dir, so it loads the same CLAUDE.md as the live session —
# including routing rules ("impl work → developer-01") and any auto-memory
# entries that nudge "delegate real work to a worker". Combined with visibility
# into the workers DB, the brain can pattern-match its own row as "another
# worker on the same topic" and bail out with a no-op result. The recursion
# guard env var only suppresses sub-spawning; it does not address self-
# duplicate-detection. This clause flips the role explicitly.
if [[ "${JC_IN_WORKER:-}" == "1" ]]; then
    ARGS+=("--append-system-prompt" "WORKER MODE: You are running as a background worker (JC_IN_WORKER=1). The task in stdin is yours to execute — authority comes from the prompt itself, not from any duplicate-check against the workers database. Do not query workers state to decide whether to execute. Do not classify yourself as redundant if you find a matching worker row (that row is you). Do not stand by because \"another worker is on it.\" The recursion guard already prevents spawning sub-workers; the additional invariant here is: do not delegate, do not duplicate-check yourself, do not no-op. Execute the brief inline.")
fi

# Structured brain-output contract: stdout MUST be a single JSON object.
# The gateway parses this object and decides whether to deliver to channel.
ARGS+=("--append-system-prompt" "GATEWAY OUTPUT CONTRACT: Your final stdout MUST be a single JSON object on a single line (no code fences, no prose before or after) with exactly these fields:
  {\"push_message_sent\": <bool>, \"message\": <string>}

Rules:
- If you used PushNotification during this task to deliver the user-facing output yourself, set push_message_sent=true. The 'message' field then becomes an audit log of what you pushed (the framework will NOT re-deliver it).
- If you did NOT use PushNotification and want the framework to relay your reply, set push_message_sent=false and put the full reply in 'message'. The framework will deliver it to the channel.
- 'message' is always required (use empty string only for genuine no-op silent runs).
- Emit ONLY the JSON object as your final output. No prefix, no suffix, no explanation, no code fence.")

exec claude "${ARGS[@]}"
