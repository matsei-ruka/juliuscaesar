#!/usr/bin/env bash
# OpenCode adapter (opencode.ai).
#
# Reads prompt from stdin, writes the assistant's reply to stdout. Model is
# $1 (optional). Remaining args are forwarded to `opencode run` (used for
# --file <path> image attachments built by OpencodeBrain.extra_args_for_event).
#
# opencode 1.16 does NOT stream text events to stdout under --format json —
# only the first `step_start` event is emitted, carrying `sessionID`. The
# reply text lives in the local SQLite store (~/.local/share/opencode/
# opencode.db, table `part`, rows tied to the last assistant message of the
# session). Tokens for the turn live on `message.data` JSON. The adapter
# captures the sessionID from stdout, runs the prompt, then queries the DB
# for both the reply text and tokens, and writes a sidecar JSON consumed by
# the runtime via $JC_USAGE_SIDECAR_PATH.

set -euo pipefail

export PATH="${HOME:-/tmp}/.local/bin:${HOME:-/tmp}/.opencode/bin:${HOME:-/tmp}/.npm-global/bin:${HOME:-/tmp}/.bun/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

MODEL="${1:-}"
shift || true
PASSTHROUGH_ARGS=("$@")
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

if [[ -n "${JC_GOAL:-}" ]]; then
    # opencode run has no native system-prompt flag; embed the goal as a
    # <system> block at the head of the user message body. The model treats
    # tagged content as system-level context.
    PROMPT=$'<system>\n'"${JC_GOAL}"$'\n</system>\n\n'"${PROMPT}"
fi

ARGS=("run" "--format" "json")

if [[ "${JC_OPENCODE_NO_TOOLS:-0}" == "1" ]]; then
    ARGS+=("--pure")
fi

RESUME="${JC_RESUME_SESSION:-${WORKER_RESUME_SESSION:-}}"
if [[ -n "$RESUME" ]]; then
    ARGS+=("--session" "$RESUME")
fi

if [[ -n "$MODEL" ]]; then
    ARGS+=("--model" "$MODEL")
fi

if (( ${#PASSTHROUGH_ARGS[@]} > 0 )); then
    ARGS+=("${PASSTHROUGH_ARGS[@]}")
fi

ARGS+=("$PROMPT")

NDJSON_TMP=$(mktemp -t opencode-ndjson.XXXXXX)
trap 'rm -f "$NDJSON_TMP"' EXIT

opencode "${ARGS[@]}" >"$NDJSON_TMP" 2>&1 || RC=$?
RC="${RC:-0}"

OPENCODE_DB="${OPENCODE_DB:-$HOME/.local/share/opencode/opencode.db}"
if [[ ! -f "$OPENCODE_DB" ]] && [[ -f "$HOME/Library/Application Support/opencode/opencode.db" ]]; then
    OPENCODE_DB="$HOME/Library/Application Support/opencode/opencode.db"
fi

SIDECAR="${JC_USAGE_SIDECAR_PATH:-}"

python3 - "$NDJSON_TMP" "$OPENCODE_DB" "$SIDECAR" "$RESUME" <<'PYEOF'
import json
import os
import sqlite3
import sys
from pathlib import Path


ndjson_path, db_path, sidecar_path, resume_session = sys.argv[1:5]

session_id = None
with open(ndjson_path, "r", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = ev.get("sessionID") or (ev.get("part", {}) or {}).get("sessionID")
        if sid:
            session_id = sid
            break

if not session_id and resume_session:
    session_id = resume_session


def query_db(sid: str) -> tuple[str, dict | None]:
    if not sid or not Path(db_path).is_file():
        return "", None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    try:
        row = conn.execute(
            "SELECT id, data FROM message "
            "WHERE session_id=? AND json_extract(data,'$.role')='assistant' "
            "ORDER BY time_created DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if row is None:
            return "", None
        message_id, raw = row
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        tokens = payload.get("tokens") if isinstance(payload, dict) else None
        parts = conn.execute(
            "SELECT data FROM part WHERE message_id=? ORDER BY time_created ASC",
            (message_id,),
        ).fetchall()
        chunks: list[str] = []
        for (part_raw,) in parts:
            try:
                part = json.loads(part_raw)
            except json.JSONDecodeError:
                continue
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "")
                if isinstance(text, str):
                    chunks.append(text)
        return "".join(chunks).strip(), tokens if isinstance(tokens, dict) else None
    finally:
        conn.close()


reply_text = ""
tokens_payload: dict | None = None
db_error: str | None = None
try:
    reply_text, tokens_payload = query_db(session_id) if session_id else ("", None)
except sqlite3.Error as exc:
    db_error = f"sqlite: {exc}"

if not reply_text:
    fallback_lines: list[str] = []
    with open(ndjson_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "text":
                part = ev.get("part", {})
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text", "")
                    if isinstance(text, str):
                        fallback_lines.append(text)
    if fallback_lines:
        reply_text = "".join(fallback_lines).strip()

sys.stdout.write(reply_text)

if sidecar_path:
    payload: dict = {}
    if session_id:
        payload["session_id"] = session_id
    usage: dict = {}
    if isinstance(tokens_payload, dict):
        if "input" in tokens_payload:
            usage["input_tokens"] = tokens_payload.get("input")
        if "output" in tokens_payload:
            usage["output_tokens"] = tokens_payload.get("output")
        cache = tokens_payload.get("cache")
        if isinstance(cache, dict):
            if "write" in cache:
                usage["cache_creation_input_tokens"] = cache.get("write")
            if "read" in cache:
                usage["cache_read_input_tokens"] = cache.get("read")
    if usage:
        payload["usage"] = usage
    if db_error and not usage:
        payload["error"] = db_error
    if payload:
        tmp = sidecar_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as out:
                json.dump(payload, out)
            os.replace(tmp, sidecar_path)
        except OSError as exc:
            sys.stderr.write(f"opencode adapter: sidecar write failed: {exc}\n")
PYEOF

exit "$RC"
