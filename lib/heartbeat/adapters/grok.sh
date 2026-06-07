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
MAX_PROMPT_BYTES=102400

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

PROMPT=$(cat)
PROMPT_LEN=${#PROMPT}
if (( PROMPT_LEN > MAX_PROMPT_BYTES )); then
    echo "grok adapter: prompt truncated from ${PROMPT_LEN} to ${MAX_PROMPT_BYTES} chars (ARG_MAX safeguard)" >&2
    PROMPT="${PROMPT:0:$MAX_PROMPT_BYTES}"
fi

# grok 0.2.x rejects prompts starting with "---" via -p (clap treats them as
# unexpected arguments even when quoted). Use --prompt-file to bypass this.
PROMPT_TMP=$(mktemp -t grok-prompt.XXXXXX)
printf '%s' "$PROMPT" > "$PROMPT_TMP"
ARGS=("--prompt-file" "$PROMPT_TMP")

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

NDJSON_TMP=$(mktemp -t grok-ndjson.XXXXXX)
STDERR_TMP=$(mktemp -t grok-stderr.XXXXXX)
trap 'rm -f "$PROMPT_TMP" "$NDJSON_TMP" "$STDERR_TMP"' EXIT

grok "${ARGS[@]}" >"$NDJSON_TMP" 2>"$STDERR_TMP" || RC=$?
RC="${RC:-0}"

if (( RC != 0 )); then
    # MODEL_SWITCH_INCOMPATIBLE_AGENT: the resumed session was started with a
    # different agent type (e.g. grok-build-plan vs cursor). Drop -r and retry
    # as a fresh session — grok's suggestion is "start_new_session".
    if grep -q "MODEL_SWITCH_INCOMPATIBLE_AGENT" "$STDERR_TMP" "$NDJSON_TMP" 2>/dev/null; then
        echo "grok adapter: MODEL_SWITCH_INCOMPATIBLE_AGENT — retrying as fresh session" >&2
        FRESH_ARGS=("--prompt-file" "$PROMPT_TMP")
        [[ -n "$MODEL" ]] && FRESH_ARGS+=("-m" "$MODEL")
        (( ${#PASSTHROUGH_ARGS[@]} > 0 )) && FRESH_ARGS+=("${PASSTHROUGH_ARGS[@]}")
        > "$NDJSON_TMP"
        > "$STDERR_TMP"
        RC=0
        grok "${FRESH_ARGS[@]}" >"$NDJSON_TMP" 2>"$STDERR_TMP" || RC=$?
    fi
    # Surface stderr + a tail of stdout so the gateway/recovery layer sees
    # the real failure (stale session, auth, network) instead of an empty
    # reply.
    if (( RC != 0 )); then
        if [[ -s "$STDERR_TMP" ]]; then
            cat "$STDERR_TMP" >&2
        fi
        if [[ -s "$NDJSON_TMP" ]]; then
            echo "--- grok stdout tail ---" >&2
            tail -n 20 "$NDJSON_TMP" >&2
        fi
        exit "$RC"
    fi
fi

SIDECAR="${JC_USAGE_SIDECAR_PATH:-}"

python3 - "$NDJSON_TMP" "$SIDECAR" "$RESUME" "$HOME" "$STDERR_TMP" <<'PYEOF'
import json
import os
import sys
import urllib.parse
from pathlib import Path


ndjson_path, sidecar_path, resume_session, home_dir, stderr_path = sys.argv[1:6]

reply_chunks: list[str] = []
session_id: str | None = None
got_end = False

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
            got_end = True
            sid = ev.get("sessionId")
            if isinstance(sid, str) and sid:
                session_id = sid

# Only fall back to the prior session id when the stream actually closed
# cleanly. A truncated/malformed stream (no `end` event) must NOT silently
# resurrect a session that may already be dead — that produces cascading
# stale-session failures on subsequent turns.
if not session_id and resume_session and got_end:
    session_id = resume_session

reply_text = "".join(reply_chunks).strip()

# Truncated stream diagnostic: grok exited 0 but emitted nothing actionable.
# Do NOT change exit code (would break the RC flow); just surface the warning
# so the gateway log shows why the reply was empty.
if not reply_chunks and not got_end:
    sys.stderr.write(
        "grok adapter: stream ended without 'end' event — possible truncation\n"
    )
    if stderr_path:
        try:
            tail = Path(stderr_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            tail = ""
        if tail.strip():
            sys.stderr.write("grok adapter: captured stderr:\n")
            sys.stderr.write(tail)
            if not tail.endswith("\n"):
                sys.stderr.write("\n")

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
    found_file = False
    for path in candidates:
        if not path.is_file():
            continue
        found_file = True
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
    if not found_file:
        sys.stderr.write(
            f"grok adapter: token file not found for session {sid} "
            f"(cwd={os.getcwd()})\n"
        )
    return 0


input_tokens = probe_tokens(session_id) if session_id else 0


def probe_images(sid: str) -> list[str]:
    """Return sorted list of image paths grok wrote for ``sid``.

    grok saves generated images to
    ~/.grok/sessions/<cwd_slug>/<sid>/images/ (Linux/macOS default) or the
    XDG variant.  Returns absolute path strings so the gateway delivery layer
    can forward them to the channel without re-probing.
    """
    if not sid:
        return []
    cwd_slug = urllib.parse.quote(os.getcwd(), safe="")
    candidates = [
        Path(home_dir) / ".grok" / "sessions" / cwd_slug / sid / "images",
        Path(home_dir) / ".local" / "share" / "grok" / "sessions" / cwd_slug / sid / "images",
    ]
    for img_dir in candidates:
        if img_dir.is_dir():
            return sorted(str(p) for p in img_dir.iterdir() if p.is_file())
    return []


images = probe_images(session_id) if session_id else []

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
    if images:
        payload["images"] = images
    tmp = sidecar_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as out:
            json.dump(payload, out)
        os.replace(tmp, sidecar_path)
    except OSError as exc:
        sys.stderr.write(f"grok adapter: sidecar write failed: {exc}\n")
PYEOF

exit "$RC"
