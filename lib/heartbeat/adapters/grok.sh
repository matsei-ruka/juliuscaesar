#!/usr/bin/env bash
# Grok adapter (xAI grok CLI 0.2.x).
#
# Reads prompt from stdin, writes assistant reply to stdout. Model is $1
# (optional). Remaining args come from GrokBrain.extra_args_for_event:
# --always-approve, --output-format streaming-json, --system-prompt-override
# <preamble>, and optional --file <path> image attachments.
#
# Brain spec aliases (resolved here so the gateway can pass "grok:fast" as
# the model spec without each call-site knowing the underlying model id):
#
#   grok            → grok-build
#   grok:grok-build → grok-build
#   grok:fast       → grok-composer-2.5-fast
#
# NDJSON schema (verified live, grok 0.2.32):
#
#   {"type":"thought","data":"..."}          # ignored
#   {"type":"text","data":"<reply chunk>"}   # concatenated → stdout
#   {"type":"end","stopReason":"...","sessionId":"<uuid>","requestId":"..."}
#
# Session id is captured from the `end` event (NOT the first event — that
# pattern is opencode's). Token usage is read post-run from the per-session
# updates.jsonl that grok writes to ~/.grok/sessions/<cwd-urlencoded>/<sid>/
# updates.jsonl (Linux path; the XDG variant ~/.local/share/grok/sessions/...
# is checked as a fallback for distros that relocate the dir).
#
# Session id, reply text, and token usage are surfaced via the sidecar at
# $JC_USAGE_SIDECAR_PATH:
#
#   {"session_id": "<uuid>",
#    "usage": {"input_tokens": <N>, "output_tokens": 0,
#              "cache_creation_input_tokens": 0,
#              "cache_read_input_tokens": 0}}
#
# Output tokens are not exposed by grok 0.2.x; zero is recorded — the §8
# lifecycle math keys off `input_tokens` so the missing field does not
# break rotation.

set -euo pipefail

export PATH="${HOME:-/tmp}/.local/bin:${HOME:-/tmp}/.npm-global/bin:${HOME:-/tmp}/.bun/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

MODEL_SPEC="${1:-}"
shift || true
PASSTHROUGH_ARGS=("$@")

if ! command -v grok >/dev/null 2>&1; then
    echo "grok CLI not installed. See https://github.com/superagent-ai/grok-cli or run 'npm i -g grok'" >&2
    exit 127
fi

# Resolve brain spec aliases. Strip optional "grok:" prefix first so worker
# paths that pass the fully-qualified spec (e.g. "grok:fast") share the same
# case arms as the bare model names.
MODEL="${MODEL_SPEC#grok:}"
case "$MODEL" in
    ""|grok-build)            MODEL="grok-build"             ;;
    fast|grok-composer-2.5-fast) MODEL="grok-composer-2.5-fast" ;;
    *)                        ;;  # passthrough — grok rejects unknown ids
esac

ARGS=("-p")

RESUME="${JC_RESUME_SESSION:-${WORKER_RESUME_SESSION:-}}"
if [[ -n "$RESUME" ]]; then
    ARGS+=("-r" "$RESUME")
fi

if [[ -n "$MODEL" ]]; then
    ARGS+=("-m" "$MODEL")
fi

if (( ${#PASSTHROUGH_ARGS[@]} > 0 )); then
    ARGS+=("${PASSTHROUGH_ARGS[@]}")
fi

PROMPT=$(cat)
ARGS+=("$PROMPT")

NDJSON_TMP=$(mktemp -t grok-ndjson.XXXXXX)
trap 'rm -f "$NDJSON_TMP"' EXIT

grok "${ARGS[@]}" >"$NDJSON_TMP" 2>&1 || RC=$?
RC="${RC:-0}"

SIDECAR="${JC_USAGE_SIDECAR_PATH:-}"

python3 - "$NDJSON_TMP" "$SIDECAR" "$RESUME" "$HOME" <<'PYEOF'
import json
import os
import sys
import urllib.parse
from pathlib import Path


ndjson_path, sidecar_path, resume_session, home_dir = sys.argv[1:5]

reply_chunks: list[str] = []
session_id: str | None = None

with open(ndjson_path, "r", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "text":
            data = ev.get("data", "")
            if isinstance(data, str):
                reply_chunks.append(data)
        elif kind == "end":
            sid = ev.get("sessionId")
            if isinstance(sid, str) and sid:
                session_id = sid

if not session_id and resume_session:
    session_id = resume_session

reply_text = "".join(reply_chunks).strip()
sys.stdout.write(reply_text)


def probe_tokens(sid: str) -> int:
    """Read the last update for ``sid`` and return effective_input_tokens.

    Checks both the macOS/Linux default (~/.grok/sessions/) and the XDG
    relocation (~/.local/share/grok/sessions/) — covers distros that move
    the grok state dir without us probing it live (Q1).
    """
    if not sid:
        return 0
    cwd_slug = urllib.parse.quote(os.getcwd(), safe="")
    candidates = [
        Path(home_dir) / ".grok" / "sessions" / cwd_slug / sid / "updates.jsonl",
        Path(home_dir) / ".local" / "share" / "grok" / "sessions" / cwd_slug / sid / "updates.jsonl",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        last_line = ""
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.strip():
                        last_line = line.strip()
        except OSError:
            continue
        if not last_line:
            continue
        try:
            payload = json.loads(last_line)
        except json.JSONDecodeError:
            continue
        meta = payload.get("_meta", {}) if isinstance(payload, dict) else {}
        totals = meta.get("totalTokens", {}) if isinstance(meta, dict) else {}
        if isinstance(totals, dict):
            val = totals.get("effective_input_tokens", 0)
            if isinstance(val, (int, float)):
                return int(val)
    return 0


input_tokens = probe_tokens(session_id) if session_id else 0

if sidecar_path:
    payload: dict = {
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
    }
    if session_id:
        payload["session_id"] = session_id
    tmp = sidecar_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as out:
            json.dump(payload, out)
        os.replace(tmp, sidecar_path)
    except OSError as exc:
        sys.stderr.write(f"grok adapter: sidecar write failed: {exc}\n")
PYEOF

exit "$RC"
