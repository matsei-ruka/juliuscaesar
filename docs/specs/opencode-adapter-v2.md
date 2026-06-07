# Opencode adapter v2 — context-aware lifecycle parity

**Status:** draft
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

NDJSON events seen during `run --format json` (per current adapter parser):

- `{"type":"text","part":{"type":"text","text":"…"}}` — assistant text output (concatenate to build the reply).
- `{"type":"session","session":{...}}` — session info, usually first frame.
- `{"type":"stats","tokens":{...}}` or similar — per-turn token usage (verify exact key on first install).

Auto-compaction (verified deepwiki 2.4): triggered internally via `isOverflow` against `usable = limit.context − 32 000 − 20 000`. Surfaces as a synthetic user message with a `CompactionPart`. The framework cannot intervene; the adapter just trusts opencode handled it.

The worker MUST verify the exact `stats` key name against a live `opencode run --format json` invocation before implementing usage parsing. The current `opencode.sh` parser only handles `type=text` events; the v2 parser must scan for the stats event and persist its payload.

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
- `pre_invoke_snapshot()` — `frozenset` of session JSONL paths under `~/.local/share/opencode/projects/<slug>/sessions/` (or wherever opencode stores them — worker probes with `opencode session list --format json` to discover, falls back to homedir search). Diff-based capture, same trick as codex.
- `capture_session_id(started_at)` — set-difference on the snapshot. If diff is empty, fall back to `opencode session list --format json` filtered by `directory == self.instance_dir` and pick the newest `time.updated >= t0`. The current implementation's two-axis query (`directory + updated`) stays as the fallback path.
- `adjust_model()` — pass-through for now. Vision upgrade handled internally by opencode model routing.

### 4.3 Adapter shell

`lib/heartbeat/adapters/opencode.sh` rewrite (~120 LOC):

1. **PATH bootstrap** — same as current (`~/.local/bin`, `~/.npm-global/bin`, `~/.bun/bin`).
2. **Prompt from stdin** — current 100KB ARG_MAX cap stays.
3. **Goal as system prompt** — opencode's `run` does not have `--append-system-prompt`, but a `<system>...</system>` prefix to the prompt body is the documented path. Adapter prepends `$JC_GOAL` (when set) wrapped in a `<system>` tag the model will treat as system context. If opencode ships a real flag by ship date, the adapter switches over.
4. **Images** — extra `--file <path>` args (repeatable) passed through from `extra_args_for_event`.
5. **Resume** — current `--session <id>` logic stays. Add `--continue` fallback only if explicit env asks.
6. **Stats extraction** — pipe NDJSON through the Python parser, collect both `type=text` (response body) and the stats event (whatever its exact type — to be confirmed). Write the response to stdout as today; write a **single line JSON** with the stats payload to a sidecar file at `$JC_USAGE_SIDECAR_PATH` (env injected by the runtime to `state/gateway/usage/<event_id>.json`).
7. **Return code semantics** — preserved: rc=0 on success, rc=127 if CLI missing.

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
- `capture_session_id` happy path: pre/post snapshot adds one file, returns its UUID stem.
- `capture_session_id` ambiguous: pre/post adds two files → falls back to `session list` JSON probe.
- `capture_session_id` empty diff + empty list → returns None.
- Sidecar usage parsing: feed a fixture stats JSON, assert `BrainResult.usage` round-trips it.

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

## 7. Open questions

- **Exact stats event type/key** — must be confirmed from a live `opencode run --format json` call. Documented as a hard worker checkpoint before any usage parsing lands.
- **Session storage location** — deepwiki says SQLite; CLI surface is `session list`. The brain only needs IDs, so SQLite path doesn't matter, but `pre_invoke_snapshot` needs a directory to diff. If sessions are SQLite-only (no per-session file), drop the snapshot path entirely and rely solely on `session list` JSON before/after diff.
- **System prompt injection mechanism** — `<system>` tag in prompt body vs a future native flag. Worker checks CLI first; if a flag exists by ship time, use it.

## 8. Non-goals

- No HTTP `serve` adapter (separate work).
- No multi-provider routing inside the adapter — opencode owns model selection logic.
- No attempt to detect or react to opencode's internal compaction event. The framework trusts opencode handled it; the next turn's usage will reflect the compact summary.
