#!/usr/bin/env bash
# OpenCode adapter (opencode.ai).
# Reads prompt from stdin, writes response to stdout. Model is $1 (optional).
#
# Non-interactive mode: opencode run --dangerously-skip-permissions --format json <prompt>
# Resume: set JC_RESUME_SESSION or WORKER_RESUME_SESSION to a session ID →
# passes --session <id>
#
# opencode run takes the message as a positional arg only — no --file or
# stdin flag exists in current releases. Linux ARG_MAX is ~2 MB, but kernels
# and shell wrappers can fail well before that. Hard-cap the prompt at 100k
# chars to keep the exec safe; emit a warning to stderr (worker log) when
# truncation happens so callers notice the lossy boundary.
#
# --format json: opencode emits NDJSON events; plain run sends TUI output to
# stderr (invisible to gateway). We parse type=text events to extract reply.
set -euo pipefail

export PATH="${HOME:-/tmp}/.local/bin:${HOME:-/tmp}/.npm-global/bin:${HOME:-/tmp}/.bun/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

MODEL="${1:-}"
MAX_PROMPT_BYTES=102400

if ! command -v opencode >/dev/null 2>&1; then
    echo "opencode CLI not installed. See https://opencode.ai" >&2
    exit 127
fi

PROMPT=$(cat)
PROMPT_LEN=${#PROMPT}
if (( PROMPT_LEN > MAX_PROMPT_BYTES )); then
    echo "opencode adapter: prompt truncated from ${PROMPT_LEN} to ${MAX_PROMPT_BYTES} chars (ARG_MAX safeguard)" >&2
    PROMPT="${PROMPT:0:$MAX_PROMPT_BYTES}"
fi

ARGS=("run" "--dangerously-skip-permissions" "--format" "json")

RESUME="${JC_RESUME_SESSION:-${WORKER_RESUME_SESSION:-}}"
if [[ -n "$RESUME" ]]; then
    ARGS+=("--session" "$RESUME")
fi

if [[ -n "$MODEL" ]]; then
    ARGS+=("--model" "$MODEL")
fi

ARGS+=("$PROMPT")

opencode "${ARGS[@]}" | python3 -c "
import sys, json
text = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        continue
    if ev.get('type') == 'text':
        part = ev.get('part', {})
        if part.get('type') == 'text':
            text.append(part.get('text', ''))
print(''.join(text), end='')
"
