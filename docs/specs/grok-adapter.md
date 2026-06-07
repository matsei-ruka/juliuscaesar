# Grok adapter — context-aware lifecycle parity

**Status:** approved — implementation
**Author:** Rachel (with Luca)
**Date:** 2026-06-07
**Branch:** `spec/grok-adapter`
**Tracks:** PR #85 (context-aware session lifecycle, already merged)
**Targets:** grok CLI 0.2.32 (xAI)

## 1. Motivation

Grok Build (xAI) ships as a headless coding agent CLI. Unlike the initial research summary
(pre-probe), the live 0.2.32 API differs on three points that affect adapter design:

1. **No session_id injection** — `-s <uuid>` does not exist; session capture is required,
   same pattern as opencode.
2. **Simpler NDJSON schema** — only three event types (`thought`, `text`, `end`), not the
   six-category schema reported in secondary sources.
3. **Native system-prompt flag** — `--system-prompt-override <PROMPT>` exists; no `<system>`
   tag injection needed.

The adapter enables grok as a selectable brain (`brain: grok`) with full parity on the
features the JC framework expects: goal anchor, session resume, token telemetry, image
attachment. Compaction is NOT provider-managed (grok has `/compact` but no auto-threshold);
the §8 rotation logic runs normally.

## 2. Scope

In scope:

- `lib/gateway/brains/grok.py` — new brain, ~150 LOC.
- `lib/heartbeat/adapters/grok.sh` — new shell adapter, ~100 LOC.
- `tests/test_brains_grok.py` — new test file.
- `docs/BRAINS.md` — add grok entry.

Out of scope:

- HTTP API (`--serve` / REST) — separate `grok_api` adapter if needed.
- Grok voice / multimodal beyond image attachment.
- X Premium+ subscription provisioning.

## 3. CLI contract (verified live — grok 0.2.32 on Chloe Mac, 2026-06-07)

### 3.1 Invocation

```bash
grok -p "<prompt>" \
     --output-format streaming-json \
     --system-prompt-override "<L1_PREAMBLE>" \
     --always-approve \
     [-r <session_id>]    # resume existing session
     [-c]                 # continue last session (alternative to -r)
     [-m <model>]         # optional model override
     [--file <path>]      # image/file attach, repeatable
```

No working-dir flag — set via subprocess `cwd`.

### 3.2 NDJSON output (`--output-format streaming-json`)

Three event types only:

```json
{"type":"thought","data":"<reasoning text>"}
{"type":"text","data":"<reply chunk>"}
{"type":"end","stopReason":"EndTurn","sessionId":"019ea13c-...","requestId":"..."}
```

- `thought` — model reasoning; **ignore**.
- `text` — assistant reply; **concatenate** all `data` fields.
- `end` — terminal event; **extract `sessionId`** here (not from an earlier event).

### 3.3 Session capture

Session ID is only available in the `end` event. Capture strategy:

```python
session_id = None
for line in stdout.splitlines():
    try:
        ev = json.loads(line)
        if ev.get("type") == "end":
            session_id = ev.get("sessionId")
    except json.JSONDecodeError:
        pass
```

No snapshot-diff, no SQLite query needed.

### 3.4 Token telemetry

Token data is written to a per-session `updates.jsonl` file:

```
~/.grok/sessions/<cwd-urlencoded>/<sessionId>/updates.jsonl
```

Last entry in the file contains `_meta.totalTokens` with field `effective_input_tokens`.
No input/output/cache breakdown — only total input tokens available. Sufficient for §8
lifecycle pressure measurement.

Post-run probe (in `grok.sh`):

```bash
UPDATES_FILE="${HOME}/.grok/sessions/$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote('$(pwd)', safe=''))")/$(SESSION_ID)/updates.jsonl"
TOKENS=$(tail -1 "$UPDATES_FILE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('_meta',{}).get('totalTokens',{}).get('effective_input_tokens',0))" 2>/dev/null || echo 0)
```

Write to sidecar `$JC_USAGE_SIDECAR_PATH` (same pattern as opencode v2).

### 3.5 System prompt

`--system-prompt-override "<L1_PREAMBLE>"` — pass full L1 preamble as CLI flag. Cleaner
than `<system>` tag injection. No body-level wrapping needed.

### 3.6 Resume flags

| Action | Flag |
|---|---|
| Resume by ID | `-r <session_id>` |
| Continue last session | `-c` |
| New session | (no flag) |

### 3.7 Model selection

Default: `grok-build` (optimal for coding tasks).
Alternative: `grok-composer-2.5-fast` (lower latency, less reasoning).
Config key: `grok_model` in `ops/gateway.yaml` brain block.

## 4. Brain contract (`lib/gateway/brains/grok.py`)

### 4.1 Config keys

```yaml
# ops/gateway.yaml
brain: grok
grok_model: grok-build          # optional, default grok-build
grok_binary: grok               # optional, default grok
```

### 4.2 Key properties

```python
class GrokBrain(BaseBrain):
    name = "grok"
    needs_l1_preamble = True
    goal_delivery = "system_prompt_override"   # → --system-prompt-override flag
    provider_managed_compaction = False        # manual /compact; §8 rotation runs
    supports_resume = True
    supports_images = True                     # via --file
```

### 4.3 Extra env keys

Brain injects into subprocess env:

- `XAI_API_KEY` (if set in instance `.env`)
- `JC_USAGE_SIDECAR_PATH` — path where adapter.sh writes token data

### 4.4 `extra_args`

- `--system-prompt-override <preamble>` if `needs_l1_preamble`
- `--file <path>` per image in payload
- `--always-approve` always
- `--output-format streaming-json` always
- `-m <grok_model>` if configured
- `-r <session_id>` if resuming, else nothing (new session)

### 4.5 Brain spec aliases

- `grok` → model `grok-build`
- `grok:grok-build` → model `grok-build`
- `grok:fast` → model `grok-composer-2.5-fast`

## 5. Adapter contract (`lib/heartbeat/adapters/grok.sh`)

Shell adapter responsibilities:

1. Build CLI invocation from env vars passed by brain.
2. Run grok, capture stdout line-by-line.
3. Extract reply (concat `text` chunks) and session_id (from `end`).
4. Probe `updates.jsonl` for token count after run.
5. Write `$JC_USAGE_SIDECAR_PATH` JSON:
   ```json
   {"input_tokens": <N>, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
   ```
   (output token count unavailable; zero is acceptable, won't break lifecycle math.)
6. Emit reply to stdout as JC envelope.

## 6. Lifecycle routing

No new branch needed in `routing.py`. `provider_managed_compaction = False` → existing path.

`RuntimeFloor.measure_context()` reads `input_tokens` from sidecar. Rotation triggers
normally when pressure exceeds threshold.

## 7. Tests (`tests/test_brains_grok.py`)

- `test_grok_reply_parse` — mock stdout with `thought`/`text`/`end` events, assert reply concat + session_id.
- `test_grok_system_prompt_flag` — assert `--system-prompt-override` present in args when `needs_l1_preamble=True`.
- `test_grok_resume_flag` — assert `-r <id>` in args when session_id provided.
- `test_grok_no_resume_flag` — assert no `-r` when no prior session.
- `test_grok_sidecar_write` — mock `updates.jsonl`, assert sidecar populated with `input_tokens`.
- `test_grok_model_alias` — `grok:fast` resolves to `grok-composer-2.5-fast`.

## 8. Open questions (for worker to resolve via probe)

**Q1** — `updates.jsonl` exact path on Linux (XDG `~/.local/share/grok/sessions/…`?).
Confirmed Mac path above; Linux may differ. Worker: run `grok -p "hello" -c` on a Linux host
with grok installed, check `find ~/.grok ~/.local/share/grok -name updates.jsonl`.

**Q2** — Does `grok -c` create a new session when no prior session exists, or error?
Worker: probe with fresh env, confirm graceful fallback to new session.

**Q3** — Max prompt length via `-p`. Very long L1 preambles (>4k chars) may need stdin
pipe instead of shell arg. Worker: test with full Rachel L1 preamble length, confirm no
`Argument list too long` error.

## 9. Prerequisites

- grok CLI installed and authenticated on target host (SuperGrok / X Premium+).
- `XAI_API_KEY` or grok's own auth token in instance `.env`.
- `which grok` must be in PATH visible to gateway process.
