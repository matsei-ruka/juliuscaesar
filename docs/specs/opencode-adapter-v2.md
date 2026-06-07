# Opencode adapter v2 — context-aware lifecycle parity

**Status:** approved — implementation
**Author:** Rachel (with Luca)
**Date:** 2026-06-07
**Branch:** `spec/opencode-v2`
**Tracks:** PR #85 (context-aware session lifecycle)
**Targets:** opencode (sst/opencode) CLI ≥ 0.39

## 1. Motivation

The opencode adapter (`lib/gateway/brains/opencode.py` + `lib/heartbeat/adapters/opencode.sh`) was last touched 2026-05-08 (71 LOC brain, 65 LOC shell). It predates PR #85 (context-aware session lifecycle, v2026.06.07.1) and the §8 token-telemetry stack. Result: opencode runs blind — no token usage, no goal injection via system prompt, no image attachments, brittle session capture.

Opencode is structurally the best lifecycle citizen of the three primary CLI brains: it auto-compacts internally at the model's `usable` threshold (model context − 32k reserved output − 20k buffer) and exposes per-session token state through `stats` and `session list`. The adapter just has to:

1. Stop fighting opencode's own compaction.
2. Report measured token usage upward so the §8 telemetry table stays populated (used for cross-brain visibility, not rotation).
3. Match the feature parity that claude/codex/pi adapters give the operator (goal anchor, images, env-key injection, session resume).

This is a v2 of the adapter only — no framework changes outside the brain and adapter files. Rotation logic in `lib/gateway/lifecycle/routing.py` learns one new branch: when `brain == "opencode"`, defer to provider auto-compaction.

## 2. Scope

In scope:

- `lib/gateway/brains/opencode.py` — full rewrite, ~220 LOC target.
- `lib/heartbeat/adapters/opencode.sh` — rewrite to emit stats and accept system-prompt + image flags.
- `lib/gateway/lifecycle/routing.py` — single-branch addition for the opencode provider-managed compaction case.
- Tests: `tests/test_brains_opencode.py` (new) + updates to `tests/test_lifecycle_routing.py`.
- Doc updates: `docs/specs/context-aware-session-lifecycle.md` cross-link.

Out of scope:

- HTTP `serve` / `attach` mode — opencode's headless server is interesting but is a separate adapter (`opencode_api`), like `codex_api`. Captured as follow-up issue.
- Sharing / forking sessions (`--share`, `--fork`).
- Vertex / Bedrock provider routing inside opencode — opencode handles provider selection from its own `~/.opencode/config.json`, the gateway only picks the model string.

## 3. CLI contract (verified 2026-06-07)

From `opencode --help` and https://opencode.ai/docs/cli/ + deepwiki (`sst/opencode/2.1`, `2.4`).

| Concern | Flag / command | Notes |
|---|---|---|
| Non-interactive run | `opencode run <prompt>` | prompt is positional, no stdin |
| Output format | `--format json` | NDJSON event stream on stdout |
| Model select | `--model <provider/model>` or `-m` | e.g. `anthropic/claude-sonnet-4-6` |
| Resume by id | `--session <id>` or `-s` | exact session UUID |
| Resume last | `--continue` or `-c` | last session in this dir |
| File attach | `--file <path>` | one per flag, repeatable |
| Working dir | `--dir <path>` | already set via subprocess cwd |
| Token stats | `opencode stats` | global cost/token summary |
| Session list | `opencode session list --format json` | per-session id + tokens + time |

NDJSON events emitted by `run --format json` (verified 2026-06-07 via web research — deepwiki sst/opencode/6.1 + CLI docs + issue #14702):

- `{"type":"step_start", ...}` / `{"type":"step_finish", ...}` — turn boundaries, include `sessionID`.
- `{"type":"text","part":{"type":"text","text":"…"}}` — assistant text output (concatenate).
- `{"type":"reasoning", ...}` — model reasoning chunks (ignore for reply).
- `{"type":"tool_use", ...}` — tool calls.
- `{"type":"error", ...}` — runtime errors.

**Resolved (Q1):** stdout NDJSON does **not** carry token usage. Issue #14702 requests `opencode stats --format json` — not shipped. Token data lives only in SQLite (`~/.local/share/opencode/opencode.db`, table `messages`, column `tokens`). The adapter MUST query SQLite directly after the run completes.

**Resolved (Q2):** session ID is available as the `sessionID` field on every event in stdout (first `step_start` is sufficient). No directory diff needed.

Auto-compaction (verified deepwiki 2.4): triggered internally via `isOverflow` against `usable = limit.context − 32 000 − 20 000`. Surfaces as a synthetic user message with a `CompactionPart`. The framework cannot intervene; the adapter just trusts opencode handled it.

## 4. Architecture

### 4.1 BrainResult.usage payload

Per `lib/gateway/lifecycle/telemetry.py`, the framework expects a normalized `ContextUsage`. Anthropic factory exists (`from_anthropic_usage`). Opencode emits in its own shape — we add a new classmethod `ContextUsage.from_opencode_stats(stats: dict)` in a follow-up commit on the lifecycle side. Until that lands, opencode brain returns the raw stats dict in `BrainResult.usage` and the runtime falls back to zero-usage (§8.2 prevents zero from overwriting last good). No regression vs current.

When the classmethod ships (same PR, separate commit), `effective_input_tokens` = `tokens.input + tokens.cache.read + tokens.cache.write` (opencode separates cache reads/writes like Anthropic), `output_tokens` = `tokens.output`. `source = "native_session"`.

### 4.2 Brain class

```python
class OpencodeBrain(Brain):
    name = "opencode"
    needs_l1_preamble = True              # opencode does NOT auto-load CLAUDE.md
    goal_delivery = "system_prompt"       # via --append-system-prompt-equivalent
                                          # implemented by adapter.sh prepending
                                          # the goal as a system message turn
```

Overrides:

- `extra_env()` — inject from instance `.env`:
  - `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GOOGLE_API_KEY`, `DEEPSEEK_API_KEY`, `GROQ_API_KEY`.
  - `JC_OPENCODE_NO_TOOLS` (`"1"` / `"0"`) — read from `override.no_tools`. Adapter consumes.
- `extra_args_for_event(event)` — map `meta.image_path` / `meta.image_paths` to `--file <path>` pairs.
- `pre_invoke_snapshot()` — **not used**. Resolved Q2: session ID comes from first stdout event. Override returns `None`.
- `capture_session_id(started_at)` — parse first NDJSON line on stdout, extract `sessionID` field. Fallback: `opencode session list --format json` filtered by `directory == self.instance_dir`, newest `time.updated >= started_at`. -40 LOC vs the snapshot-diff approach.
- `adjust_model()` — pass-through for now. Vision upgrade handled internally by opencode model routing.

### 4.3 Adapter shell

`lib/heartbeat/adapters/opencode.sh` rewrite (~120 LOC):

1. **PATH bootstrap** — same as current (`~/.local/bin`, `~/.npm-global/bin`, `~/.bun/bin`).
2. **Prompt from stdin** — current 100KB ARG_MAX cap stays.
3. **Goal as system prompt** — opencode's `run` does not have `--append-system-prompt`, but a `<system>...</system>` prefix to the prompt body is the documented path. Adapter prepends `$JC_GOAL` (when set) wrapped in a `<system>` tag the model will treat as system context. If opencode ships a real flag by ship date, the adapter switches over.
4. **Images** — extra `--file <path>` args (repeatable) passed through from `extra_args_for_event`.
5. **Resume** — current `--session <id>` logic stays. Add `--continue` fallback only if explicit env asks.
6. **Reply assembly** — pipe NDJSON through the Python parser, collect `type=text` events into the response body. Capture `sessionID` from the first event with that field.
7. **Token usage via SQLite probe** — after `opencode run` exits, run:

   ```bash
   sqlite3 -json "$OPENCODE_DB" \
     "SELECT tokens FROM messages WHERE session_id='${SESSION_ID}' ORDER BY rowid DESC LIMIT 1"
   ```

   Default `OPENCODE_DB="$HOME/.local/share/opencode/opencode.db"` (XDG on Linux). On macOS the worker verifies the path and writes a fallback `~/Library/Application Support/opencode/opencode.db`. Write the JSON result to `$JC_USAGE_SIDECAR_PATH`. If the probe fails (DB locked, row missing), write `{"error":"...", "session_id":"..."}` — the runtime treats absent/error sidecar as zero usage (§8.2 guard prevents regression).
8. **Return code semantics** — preserved: rc=0 on success, rc=127 if CLI missing.

The Python parser block stays inline (no new file). The sidecar path is new.

### 4.4 Runtime read of sidecar

`lib/gateway/brains/base.py` already builds `stderr_path` / `stdout_path` per invocation in a known dir. Add:

```python
usage_dir = self.instance_dir / "state" / "gateway" / "usage"
usage_dir.mkdir(parents=True, exist_ok=True)
usage_path = usage_dir / f"{event.id}-{os.getpid()}.json"
env["JC_USAGE_SIDECAR_PATH"] = str(usage_path)
```

After `subprocess.communicate`, if the sidecar file exists, load it and pass into `BrainResult.usage`. This is a base-class change for all adapters that want to report usage via sidecar (opencode, future codex non-API). Claude/pi/codex_api unaffected because they don't write to the sidecar.

Why sidecar over stdout: opencode's stdout is the user-facing reply. Mixing telemetry there is fragile (escaping, partial reads). Sidecar is atomic and trivial to delete after read.

### 4.5 Routing — defer rotation to provider

`lib/gateway/lifecycle/routing.py` already gates rotation on usage telemetry. Add:

```python
PROVIDER_MANAGED_COMPACTION_BRAINS = {"opencode"}

def should_rotate(brain, profile, usage):
    if brain in PROVIDER_MANAGED_COMPACTION_BRAINS:
        return False  # opencode handles its own compaction at usable threshold
    # ... existing logic ...
```

The framework still records the telemetry (effective_input_tokens) so cross-brain dashboards remain accurate — only the rotation trigger is skipped.

## 5. Test plan

`tests/test_brains_opencode.py` (new):

- `extra_env` returns expected keys when `.env` has them; empty when missing.
- `extra_args_for_event` produces `--file <p>` pairs for `image_path`, `image_paths`, both, neither.
- `capture_session_id` happy path: first NDJSON line has `sessionID` field → returns it.
- `capture_session_id` no `sessionID` in stream: falls back to `session list` JSON probe.
- `capture_session_id` empty stream + empty list → returns None.
- SQLite probe: fixture `opencode.db` with one message row, assert sidecar JSON contains expected tokens payload.
- SQLite probe failure: missing DB file → sidecar contains `{"error":...}`, runtime treats as zero usage.
- Sidecar usage parsing: feed a fixture sidecar JSON, assert `BrainResult.usage` round-trips it.

`tests/test_lifecycle_routing.py` (update):

- `should_rotate("opencode", profile, usage)` returns False regardless of profile / usage.
- `should_rotate("claude", profile, usage)` unchanged.

Integration probe (manual, doc only):

- On a host with opencode installed, run `opencode run --format json --model anthropic/claude-sonnet-4-6 "hi"` and confirm the stats event payload matches the spec — update §4.1 if not.

## 6. Rollout

1. Spec merged on `spec/opencode-v2`.
2. Worker implements on same branch, opens PR.
3. CI green + smoke test on a host that has opencode (currently none — install on .246 dev shell first, NOT a production agent).
4. Tag `v2026.06.XX.1`, ship via `jc update`.
5. No agent in the fleet currently routes to opencode primary → no production risk on rollout. Sergio clones (.119/.120) are wired to install opencode for backup but it's not installed yet; install once v2 ships so they get the new adapter from day one.

## 7. Open questions — resolved 2026-06-07

- **~~Exact stats event type/key~~** → **stdout has no tokens.** SQLite probe required (§4.3 step 7). Issue #14702 tracks upstream feature request.
- **~~Session storage location~~** → **SQLite-only** (`opencode.db`, table `messages`). Session ID exposed in stdout per-event `sessionID` field. Snapshot diff dropped from spec.
- **~~System prompt injection~~** → **confirmed no native flag.** `<system>` tag in prompt body stays. Verified against `opencode run --help` (only `--continue`, `--session`, `--fork`, `--model`, `--variant`, `--format`).

Remaining worker checkpoints (verify on live install, not blockers):

- Exact `tokens` column schema on `messages` table (one row per turn vs aggregated).
- macOS `OPENCODE_DB` path (Linux XDG confirmed `~/.local/share/opencode/opencode.db`).

## 8. Non-goals

- No HTTP `serve` adapter (separate work).
- No multi-provider routing inside the adapter — opencode owns model selection logic.
- No attempt to detect or react to opencode's internal compaction event. The framework trusts opencode handled it; the next turn's usage will reflect the compact summary.
