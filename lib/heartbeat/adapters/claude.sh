#!/usr/bin/env bash
# Claude Code adapter.
# Reads prompt from stdin, writes response to stdout. Model is $1 (optional).
# Runs `claude -p` in a fresh non-interactive session — DOES NOT touch the main
# interactive session (the one serving Telegram).
set -euo pipefail

export PATH="${HOME:-/tmp}/.local/bin:${HOME:-/tmp}/.npm-global/bin:${HOME:-/tmp}/.bun/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

MODEL="${1:-}"

# Resolve JC internal aliases (brain:model) to valid claude CLI model IDs.
# The workers runner passes worker.model verbatim; that field stores the JC
# alias format (e.g. "claude:opus", "claude:sonnet-4-6", "claude:opus-4-7-1m").
# Strip the "claude:" prefix, then map short aliases to canonical IDs.
if [[ "$MODEL" == claude:* ]]; then
    MODEL="${MODEL#claude:}"
fi
case "$MODEL" in
    opus|opus-4-7|opus-4-7-1m|opus-4-8) MODEL="opus"   ;;
    sonnet|sonnet-4-6|sonnet-4-7)        MODEL="sonnet" ;;
    haiku|haiku-4-5*)                    MODEL="haiku"  ;;
esac

# Use subscription auth (no API key env var set). No --channels flag: scheduled
# runs should never bind to the telegram channel.
ARGS=(
    "-p"
    "--dangerously-skip-permissions"
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

# Structured brain-output contract: stdout MUST be a single JSON object.
# The gateway parses this object and decides whether to deliver to channel.
# Worker mode override. When invoked from `jc-workers _run`, JC_IN_WORKER=1.
# Without this, the worker auto-loads the instance CLAUDE.md, applies the
# live-session routing logic ("impl work -> developer-01"), queries the
# workers DB, sees itself, classifies as duplicate and exits with no work
# done. The recursion guard prevents respawning sub-workers but does not
# stop the self-bail. This system-prompt suffix flips the model into
# executor mode.
#
# The suffix also suspends instance-level "always spawn a worker / reply
# confirming the spawn" rules that live in CLAUDE.md, RULES.md, and
# Claude Code project memory (~/.claude/projects/<slug>/memory/). Those
# rules target the live session; a worker that reads them as-is and
# cannot spawn (recursion guard) tends to emit a "Worker confirmed
# running…" acknowledgment in lieu of the actual work and exit cleanly.
# Observed 2026-05-12 in ethan_zhang instance (workers #31, #32).
if [[ -n "${JC_IN_WORKER:-}" ]]; then
    ARGS+=("--append-system-prompt" "WORKER MODE: You ARE the executor. The brief in stdin is your task. Execute it inline and emit a result. Do NOT triage the brief, do NOT query 'jc workers list', do NOT delegate to another worker, do NOT classify yourself as a duplicate or as redundant. The recursion guard already prevents sub-workers; the additional invariant is: do not stand by, do not no-op. If the brief is unclear, do your best with the information you have and emit a partial result with a question — do not exit silently.

Any rule in CLAUDE.md, RULES.md, or auto-loaded Claude Code memory (~/.claude/projects/<slug>/memory/) that tells you to 'spawn a worker', 'route to developer-01/developer-02', 'always delegate code work', or 'reply immediately confirming the spawn' is suspended in this session. Those rules target the live chat session, not you. You ARE the worker the live session would have spawned.

Do NOT emit a status acknowledgment like 'Worker confirmed running, starting now' or 'Spawned worker X for Y' in lieu of execution. That is a no-op disguised as progress and is the exact failure pattern this clause exists to prevent. Work through the brief sequentially — first action, then next, then next — and emit your final result only when every step is complete or you hit a true blocker you cannot unblock alone.")
fi

# Task-goal anchor (PR #65). The gateway sets JC_GOAL to the current task's
# goal text for task conversations; deliver it as a system-prompt directive so
# the model stays anchored across turns without re-explaining the task. Placed
# before the output contract so the contract remains the last (governing)
# directive.
if [[ -n "${JC_GOAL:-}" ]]; then
    ARGS+=("--append-system-prompt" "CURRENT GOAL (your active task — keep all work aligned to this until told otherwise):
$JC_GOAL")
fi

ARGS+=("--append-system-prompt" "GATEWAY OUTPUT CONTRACT: Your final stdout MUST be a single JSON object on a single line (no code fences, no prose before or after) with exactly these fields:
  {\"push_message_sent\": <bool>, \"message\": <string>}

Rules:
- If you used PushNotification during this task to deliver the user-facing output yourself, set push_message_sent=true. The 'message' field then becomes an audit log of what you pushed (the framework will NOT re-deliver it).
- If you did NOT use PushNotification and want the framework to relay your reply, set push_message_sent=false and put the full reply in 'message'. The framework will deliver it to the channel.
- 'message' is always required (use empty string only for genuine no-op silent runs).
- Emit ONLY the JSON object as your final output. No prefix, no suffix, no explanation, no code fence.")

exec claude "${ARGS[@]}"
