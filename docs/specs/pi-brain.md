# Spec: pi.dev brain

**Status:** Draft
**Date:** 2026-05-14
**Branch base:** `main`
**Owner:** TBD

## Goal

Add `pi` (pi.dev) as a first-class gateway brain alongside `claude`, `codex`,
`opencode`, `gemini`, and `aider`. pi.dev is a minimal terminal coding harness
that invokes LLMs via subscription OAuth or API keys and ships with a print
mode (`pi -p`) suitable for non-interactive subprocess invocation.

JuliusCaesar already wraps native CLI tools as brains — Claude Code (`claude
-p`), Codex CLI (`codex exec -`), OpenCode (`opencode run`). pi.dev follows the
same execution model: invoke the native binary as a subprocess, feed the prompt
via stdin or positional arg, capture stdout.

This spec defines the Python brain wrapper, the shell adapter, config wiring,
session capture strategy, tests, and operator-facing behavior needed for
`default_brain: pi` and `channels.<name>.brain: pi` to work in production.

## Non-goals

- Do not implement OAuth login for pi. `pi` / `/login` remains operator-owned.
- Do not replace pi's tool system or session format. Invoke `pi` as-is and
  consume its stdout.
- Do not change the default instance brain away from `claude`.
- Do not require pi to be the operator's primary interactive tool. Gate pi
  behind the gateway's multi-brain router alongside other brains.
- Do not add pi-specific image or multimodal support unless `pi -p` natively
  supports image inputs and the capability matrix can be updated with tests.
- Do not ship pi with the JuliusCaesar framework. pi is an operator-installed
  dependency (like `codex` or `opencode`).

## pi.dev summary

pi.dev (`pi`) is a terminal coding harness installed via npm:

```bash
npm install -g @earendil-works/pi-coding-agent
```

Key features relevant to subprocess invocation:

| Feature | Detail |
|---------|--------|
| **Print mode** | `pi -p "prompt"` — non-interactive, prints response to stdout and exits |
| **Model selection** | `pi --model <pattern>` or `pi --model provider/id` with optional `:<thinking>` suffix |
| **Thinking level** | `pi --thinking off\|minimal\|low\|medium\|high\|xhigh` |
| **Tools control** | `pi --tools <list>` to allowlist; `pi --no-tools` to disable all |
| **No-builtin-tools** | `pi --no-builtin-tools` to disable default tools but allow extension/custom tools |
| **Session management** | `pi --session <path\|id>`, `pi --resume`, `pi --fork <path\|id>` |
| **Context files** | Auto-loads `CLAUDE.md` / `AGENTS.md` from cwd and parent dirs |
| **System prompt** | `pi --system-prompt <text>` to replace; `pi --append-system-prompt <text>` to append |
| **Extensions off** | `pi --no-extensions` to disable extension/skill/prompt-template discovery |
| **Context files off** | `pi --no-context-files` / `-nc` to skip AGENTS.md/CLAUDE.md loading |
| **Provider** | `pi --provider <name>` to select provider (anthropic, openai, google, etc.) |
| **API key** | `pi --api-key <key>` to override env vars |
| **Piped stdin** | `cat README.md \| pi -p "Summarize"` — merges stdin into the prompt |
| **JSON mode** | `pi --mode json` outputs all events as JSON lines |

### Subscription & auth

pi supports OAuth subscription login (`pi /login` → select provider) for:

- Anthropic Claude Pro/Max
- OpenAI ChatGPT Plus/Pro (Codex)
- GitHub Copilot

It also supports API key auth via environment variables (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, etc.) or `pi --api-key <key>`.

For JuliusCaesar's purposes, pi must already be authenticated (logged in or
env var set) before the gateway invokes it. The gateway does not manage pi auth
— it passes through the operator's existing auth state.

### Session model

pi sessions are JSONL files in `~/.pi/agent/sessions/` organized by working
directory. Each entry has `id` and `parentId`, enabling tree-structured
branching.

- `pi --session <path|id>` — use specific session file or partial UUID
- `pi --resume` — browse and select past session
- `pi --fork <path|id>` — fork session into new session
- `pi -c` — continue most recent session
- `pi --no-session` — ephemeral mode (don't save)

For JuliusCaesar resume: the brain wrapper must be able to capture a session
id from a pi invocation and pass it to the next via `pi --session <id>`. The
challenge is that pi's session ID is a UUID used in filenames under
`~/.pi/agent/sessions/<cwd-slug>/<uuid>.jsonl`, and pi in print mode may or
may not write a session file depending on `--no-session`.

## Current behavior

JuliusCaesar has seven brain implementations:

1. **claude** — invokes `claude -p`, captures session from
   `~/.claude/projects/<slug>/<uuid>.jsonl` timestamp
2. **codex** — invokes `codex exec -`, captures session from
   `~/.codex/sessions/` pre/post snapshot
3. **codex_api** — direct API path, stateless with transcript priming
4. **opencode** — invokes `opencode run`, captures session from
   `opencode session list --format json`
5. **gemini** — invokes `gemini` CLI
6. **openrouter** — OpenRouter API
7. **aider** — invokes `aider`, captures session from
   `<instance>/state/gateway/aider-sessions/`

No `pi` brain exists. A user selecting `pi` as `default_brain` would get a
config validation error ("unsupported brain").

## Desired behavior

### Brain lifecycle

1. **Register** `pi` in `SUPPORTED_BRAINS` and the `_BRAIN_REGISTRY`.
2. **Validate** `brains.pi.*` config overrides (bin, timeout_seconds, extra_args).
3. **Invoke** `pi -p <prompt>` (print mode) as a subprocess from the instance
   directory.
4. **Capture** session id after invocation for conversation resume.
5. **Resume** via `pi --session <session-id> -p <prompt>` on subsequent turns.

### Print mode invocation

pi's print mode (`pi -p`) is the natural non-interactive entry point:

```bash
pi -p "full prompt from stdin/arg"
```

Unlike `claude -p` (which reads stdin when `-p` is used without a positional),
pi's `-p` takes the message as a CLI argument. For long prompts (which can
include the full L1 preamble + metadata + user message, potentially multiple
KB), this hits ARG_MAX limits.

Options for feeding the prompt:

1. **Positional argument** (simplest): `pi -p "<prompt>"` — subject to ARG_MAX
   (~128KB-2MB depending on OS). OpenCode uses this approach with a 100KB cap.
2. **Piped stdin + `-p`**: pi merges piped stdin into the prompt in print
   mode. `echo "Summarize this" | pi -p @README.md` — but `-p` consumes the
   positional arg, and stdin merges *with* it, not replaces. We could pipe the
   full prompt and use `-p` with a short instruction.
3. **`--append-system-prompt` + stdin**: set context via system prompt, pipe
   user message via stdin. pi's `--append-system-prompt` adds to the system
   prompt; `--system-prompt` replaces it.

**Recommended approach (Option A — positional arg with size cap):**

```bash
pi -p "<prompt>" [--model <model>] [--thinking <level>] \
   [--no-tools] [--no-extensions] [--no-context-files] \
   [--session <id>]
```

Cap the prompt at ~100KB (matching the OpenCode adapter), truncating with a
log warning. This is the simplest approach and matches existing brain patterns.

**If ARG_MAX proves problematic in practice**, fall back to Option B:
`--system-prompt` for preamble + piped stdin for user message.

### Context files and L1 preamble

pi auto-loads `CLAUDE.md` / `AGENTS.md` from cwd and parent dirs. Since
JuliusCaesar instances have `CLAUDE.md` at the instance root, pi invoked from
the instance directory would automatically pick up the instance's CLAUDE.md.

This means pi *already has* context parity with Claude — it reads the same
`CLAUDE.md` that Claude auto-loads, including all `@memory/L1/*.md` imports.

**Decision: set `needs_l1_preamble = False`** (like ClaudeBrain). pi
auto-loads `CLAUDE.md` from the cwd, so injecting the gateway preamble on top
would double-load L1 content. The gateway clock and metadata block should still
be injected, but via a different mechanism — either:

- `--append-system-prompt` for gateway-only metadata (clock, routing block,
  voice instructions, known chats)
- Or `--system-prompt` to fully replace (and include CLAUDE.md content
  ourselves if we want control)

**Recommended approach**: use `--no-context-files` to suppress pi's own
context loading, then pipe the full gateway preamble (L1 + clock + metadata +
user message) as the sole prompt. This gives the gateway full control over
what pi sees, avoids double-loading, and keeps the prompt architecture
consistent with codex/opencode brains.

Correction: if we use `--no-context-files`, pi won't load the instance's
CLAUDE.md. We'd then need to include its content in the gateway preamble,
which is what the existing non-Claude brains already do via `render_preamble()`.
So the simplest correct path is:

- `needs_l1_preamble = True` (like codex, opencode)
- pi invoked with `--no-context-files --no-extensions`
- Gateway preamble includes full L1, clock, metadata, voice instructions
- pi's tool set controlled by brain config

This avoids edge cases where pi's CLAUDE.md parsing differs from JC's and
keeps the prompt contract consistent across non-Claude brains.

### Session capture

pi stores sessions at:

```
~/.pi/agent/sessions/<cwd-slug>/<uuid>.jsonl
```

Where `<cwd-slug>` is derived from the working directory path (similar to how
Claude Code uses `~/.claude/projects/<slug>/`).

Capture strategy (order of preference):

1. **Pre/post snapshot** of `~/.pi/agent/sessions/<cwd-slug>/`. Take a
   snapshot of `*.jsonl` files before invocation; after invocation, the new
   file is our session id (same approach as CodexBrain). Extract the UUID
   from the filename stem.
2. **Fallback**: if no session file was created or multiple appeared, return
   `None` and let the next turn use transcript priming.

**Important**: pi's `-p` (print) mode may not write a session file at all
(it's designed for one-off queries). The session capture must handle this
gracefully. If `pi -p` doesn't persist sessions, we fall through to
transcript priming — which the base `Brain.invoke()` already handles for
`needs_l1_preamble=True` brains.

If pi in print mode does write a session (test this first), then `--session
<id>` on the next invocation resumes it.

**Open question**: does `pi -p` write session files? If not, we may need to
use `--mode json` for invocation and parse the session id from the JSONL
output, or accept that pi sessions are ephemeral and always use transcript
priming.

### Output contract

pi in print mode writes the model's response to stdout. The gateway's
`BrainOutput` parser already handles plain-text stdout (fallback: entire
stdout becomes `message` with `push_message_sent=false`).

To get structured output with `push_message_sent` support, we need pi to emit
the JSON contract. Since pi doesn't have a `--append-system-prompt` equivalent
that *only* affects the final output format (it affects the system prompt
which shapes behavior), we have two options:

1. **Inject the output contract into the preamble** (like codex.sh does by
   prepending to stdin). Instruct pi to emit JSON. The `BrainOutput` parser
   handles recovery if pi emits prose + JSON (embedded contract extraction).
2. **Accept plain-text stdout** and let the gateway deliver it as-is. No
   `push_message_sent` support.

**Recommended**: Option 1. Inject the gateway output contract into the
preamble so pi knows to emit structured JSON. The contract is already
well-tested with other brains.

### Model mapping

pi accepts `--model <pattern>` where pattern can be:
- A short name: `sonnet`, `opus`, `haiku`
- A provider-qualified id: `anthropic/claude-sonnet-4-6`
- With thinking suffix: `sonnet:high`

JuliusCaesar brain specs are `<brain>:<model>` (e.g., `pi:sonnet`). The
adapter script must map JC model aliases to pi-compatible model patterns.

Aliases to add:
```python
"pi": "pi",
"pi-sonnet": "pi:sonnet",
"pi-opus": "pi:opus",
"pi-haiku": "pi:haiku",
"pi-gpt5": "pi:gpt-5.4",
```

The adapter script strips the `pi:` prefix and passes the model name to
`pi --model <name>`.

For API-key-based providers, the operator must have the key set in the
environment. For subscription providers, pi must be logged in via `/login`
before gateway starts.

### Tools

pi ships with built-in tools: `read`, `bash`, `edit`, `write`, `grep`, `find`,
`ls`. For gateway chat, we likely want to restrict or disable tools — a chat
brain shouldn't edit files unless explicitly asked.

Configuration:

- `pi --no-tools` — disable all tools
- `pi --tools read,grep,find,ls` — read-only tools
- `pi --no-builtin-tools` — disable built-in but keep extension tools

Brain override config:
```yaml
brains:
  pi:
    tools: "read,write,edit,bash"  # or "none" for --no-tools
    no_builtin_tools: false
```

Default for gateway chat: `--no-tools` (read-only, no file edits). Workers
may override with full tool access.

### pi extensions

pi supports TypeScript extensions, skills, and prompt templates. For gateway
invocations, we should disable extension loading by default (`--no-extensions`)
to keep invocations deterministic and avoid interference from the operator's
interactive pi config.

Override available via `brains.pi.extra_args` if an operator wants to load
specific extensions.

### Thinking level

pi supports `--thinking off|minimal|low|medium|high|xhigh`. The gateway
doesn't have a per-message thinking level concept today, but if the brain
override config supports it:

```yaml
brains:
  pi:
    thinking: "high"
```

Otherwise, default to whatever pi's default is (which respects
`settings.json`).

### Capability matrix

| Brain     | Text | Images | Tools | File edits | Gateway chat default |
|-----------|------|--------|-------|------------|----------------------|
| pi        | yes  | yes*   | yes   | yes        | yes, --no-tools      |

*pi supports image pasting in interactive mode. Non-interactive image support
depends on pi's CLI accepting image paths (TBD). For initial implementation,
flag image support as "no" until tested.

## Implementation plan

### Phase 1 — Shell adapter

**File:** `lib/heartbeat/adapters/pi.sh`

Write the adapter script that `Brain.invoke()` spawns. It must:

1. Read the full prompt from stdin.
2. Validate that `pi` is installed (`command -v pi`).
3. Map JC model aliases to pi-compatible model patterns (strip `pi:` prefix).
4. Build the `pi -p` command line:
   ```bash
   pi -p "$PROMPT" \
      --no-context-files \
      --no-extensions \
      ${MODEL:+--model "$MODEL"} \
      ${TOOLS:+--tools "$TOOLS"} \
      ${SESSION:+--session "$SESSION"} \
      ${THINKING:+--thinking "$THINKING"} \
      [extra_args...]
   ```
5. Cap the prompt at ~100KB with a stderr warning on truncation (matching
   opencode.sh).
6. Handle exit codes: non-zero → `AdapterFailure`.

**Acceptance:**
```bash
# Manual: adapter reads stdin and produces stdout
echo "Say hello in JSON: {\"push_message_sent\": false, \"message\": \"hi\"}" | \
  JC_INSTANCE_DIR=/tmp/test-instance bash lib/heartbeat/adapters/pi.sh sonnet

# Returns JSON, exit 0
```

### Phase 2 — Python brain wrapper

**File:** `lib/gateway/brains/pi.py`

Create `PiBrain(Brain)`:

```python
class PiBrain(Brain):
    name = "pi"
    needs_l1_preamble = True  # Gateway provides full L1 via preamble

    def extra_env(self) -> dict[str, str]:
        # pi reads auth from ~/.pi/ or env vars; inject keys from instance .env
        ...

    def extra_args_for_event(self, event: Event) -> tuple[str, ...]:
        # Pass --thinking, --tools, etc. from brain override config
        ...

    def pre_invoke_snapshot(self) -> frozenset[str]:
        # Snapshot session JSONL files before invocation
        ...

    def capture_session_id(self, started_at: str) -> str | None:
        # Diff pre/post snapshots to find new session file
        ...
```

Session capture uses the same pre/post snapshot pattern as `CodexBrain`:
snapshot `~/.pi/agent/sessions/<cwd-slug>/` before invocation, find the
new JSONL file after, extract UUID from stem.

**Acceptance:**
```bash
pytest tests/gateway/test_pi_brain.py
```

Required test cases:
- `PiBrain.name == "pi"`
- `PiBrain.needs_l1_preamble == True`
- `pre_invoke_snapshot` returns a frozenset of paths
- `capture_session_id` returns UUID of newly created session file
- `capture_session_id` returns `None` when no new file appears
- `capture_session_id` returns `None` when multiple new files appear
- `extra_env` injects API keys from instance `.env`
- `extra_args_for_event` includes `--no-tools` by default

### Phase 3 — Registration and config

**Files:**
- `lib/gateway/brains/__init__.py` — add `PiBrain` to exports
- `lib/gateway/brains/dispatch.py` — add `"pi": PiBrain` to `_BRAIN_REGISTRY`
- `lib/gateway/config.py` — add `"pi"` to `SUPPORTED_BRAINS` and
  `SUPPORTED_UNSAFE_FALLBACK_BRAINS`
- `lib/gateway/brains/aliases.py` — add pi aliases
- `lib/gateway/capabilities.py` — add pi to capability matrix

**Acceptance:**
```bash
pytest tests/gateway/test_brain_specs.py tests/gateway/test_config_env.py
```

Required test cases:
- `default_brain: pi` passes config validation
- `channels.telegram.brain: pi` passes config validation
- `default_brain: pi:sonnet` preserves model
- `/brain pi-sonnet` resolves to `pi:sonnet`
- `pi` appears in `supported_brains()` output

### Phase 4 — Session capture edge cases

**Files:**
- `lib/gateway/brains/pi.py`
- `tests/gateway/test_pi_brain.py`

Work:
1. Handle the case where `pi -p` doesn't write a session file (ephemeral mode).
   Return `None` → next turn gets transcript priming.
2. Handle concurrent pi invocations (multiple new session files → return
   `None`).
3. Handle the cwd-slug computation correctly (must match pi's internal
   derivation from `--session-dir` or default `~/.pi/agent/sessions/`).
4. If pi supports `--no-session`, consider using it and always relying on
   transcript priming (simpler, no session capture complexity).

**Open question**: does `pi -p` write session files? Test this before deciding
session capture strategy.

**Acceptance:**
```bash
pytest tests/gateway/test_pi_brain.py tests/gateway/test_transcripts_runtime.py
```

Required test cases:
- No session file created → `capture_session_id` returns `None`
- Next turn with `None` session → transcript priming applied
- Concurrent unrelated pi session → not captured
- Session created by this invocation → captured

### Phase 5 — Gateway output contract injection

**Files:**
- `lib/gateway/brains/pi.py` — override `prompt_for_event` to inject contract

Work:
1. Append the gateway output contract to the prompt (matching codex.sh pattern).
2. Ensure `BrainOutput.parse_brain_output()` handles pi's output correctly.
3. Test with a real pi invocation to verify pi obeys the contract.

**Acceptance:**
```bash
pytest tests/gateway/test_brain_output.py
```

Required test cases:
- pi emits valid JSON output contract → parsed correctly
- pi emits prose + JSON → embedded contract extraction succeeds
- pi emits only prose → delivered as-is as plain text

### Phase 6 — End-to-end integration

**Files:**
- No new files; integration test across existing suite

Work:
1. Manual smoke test: `jc gateway enqueue --source telegram --content "hello"`
   with `default_brain: pi`.
2. Test conversation continuity across multiple turns.
3. Test that `brains.pi.*` overrides work (custom bin path, extra args).
4. Test `jc doctor` reports pi availability.

**Acceptance:**
```bash
# Manual smoke
jc doctor                          # reports pi CLI installed/version
jc gateway enqueue --source telegram --conversation-id pi-smoke --content "hello"
jc gateway work-once               # pi invoked, response delivered
```

## Rollout plan

1. Land Phase 1 (adapter) and Phase 2 (wrapper) together. These are the
   minimum for `default_brain: pi` to work.
2. Land Phase 3 (registration) before merging to `main`.
3. Land Phase 4 (session capture) before recommending pi for multi-turn chat.
4. Land Phase 5 (output contract) for push-notification support.
5. Phase 6 (integration) as the merge-gate smoke test.

## Backward compatibility

- Existing brain configs are unaffected.
- `pi` is new; no existing instances have `default_brain: pi` or
  `channels.<name>.brain: pi`.
- No migration needed for existing instances.

## Security and safety

- pi invoked from the gateway must not be an interactive TUI session. `-p`
  (print mode) enforces this.
- Gateway chat should default to `--no-tools` to prevent unintended file edits.
  Operators can override via `brains.pi.extra_args` or a future `tools` config
  key.
- pi's auth state (OAuth tokens, API keys in `~/.pi/`) must be readable by the
  gateway process user. No additional credential passing.
- If `brains.pi.bin` is set to a custom path, validate it's executable at
  startup via `Brain.validate()`.
- pi's extension system must be disabled (`--no-extensions`) by default to
  prevent arbitrary operator extension code from running in gateway context.

## Open questions

1. **Does `pi -p` write session files?** This determines whether we use
   pre/post snapshot capture or always fall back to transcript priming.
   Answer this before Phase 4 implementation.
2. **Can pi accept a prompt larger than ARG_MAX via stdin?** If `pi -p`
   supports piped stdin merging, we could avoid the 100KB cap. Test: `cat
   largefile.txt | pi -p "summarize"`. If this works, Option B
   (`--system-prompt` for preamble + stdin for user message) becomes viable.
3. **How does pi derive the cwd-slug for session directories?** We need to
   match this to find session files. Reverse-engineer from
   `~/.pi/agent/sessions/` or check pi source.
4. **Should `pi --no-context-files` also suppress `.pi/SYSTEM.md` loading?**
   The gateway preamble provides the system prompt; we want pi's own
   project-level system prompts disabled to avoid interference.
5. **Can pi's `--mode json` be used to parse structured output instead of
   relying on the JSON output contract?** This would give us parseable
   JSONL with tool calls and final message, similar to how we could
   potentially capture session id from the JSONL metadata. Trade-off:
   more complex parsing vs. simple stdout capture.
6. **Should pi chat invocations use subscription auth (pi's `/login`) or
   API keys from the instance `.env`?** The operator's choice. If they use
   pi interactively and are already logged in, the subprocess inherits auth.
   If they want a separate API key, they can set it in `.env` and pi will
   pick it up via env var.
7. **Thinking level: should the gateway expose a `brains.pi.thinking`
   config key?** pi supports `--thinking off|minimal|low|medium|high|xhigh`.
   Adding a config key is straightforward; default to unset (pi's default).

## Definition of done

pi is production-ready as a gateway brain when all are true:

- `default_brain: pi` works and routes to `pi -p <prompt>`.
- `channels.telegram.brain: pi` works.
- `channels.telegram.brain: pi:sonnet` preserves the model.
- pi receives full L1 preamble, clock, metadata block, and voice instructions
  via gateway preamble (not double-loaded with pi's own CLAUDE.md parsing).
- Session capture returns correct session id or `None` safely.
- Transcript priming gives continuity when native session resume is unavailable.
- `brains.pi` config overrides (bin, timeout_seconds, extra_args) work.
- `pi -p` invocations use `--no-extensions` by default.
- Gateway chat invocations use `--no-tools` by default (configurable).
- Output contract injection works (pi emits structured JSON or plain text
  gracefully handled).
- `jc doctor` reports pi CLI presence and version.
- Alias `/brain pi-sonnet` resolves correctly.
- Targeted tests are green:

```bash
pytest \
  tests/gateway/test_pi_brain.py \
  tests/gateway/test_brain_specs.py \
  tests/gateway/test_brain_output.py \
  tests/gateway/test_transcripts_runtime.py \
  tests/gateway/test_config_env.py
```
