# Spec: pi.dev brain

**Status:** Draft
**Date:** 2026-05-14
**Branch base:** `main`
**Owner:** TBD — assign before merge

## Goal

Add `pi` (pi.dev) as a first-class gateway brain alongside `claude`, `codex`,
`opencode`, `gemini`, and `aider`. pi.dev is a terminal coding harness that
invokes LLMs via subscription OAuth or API keys and ships with a print mode
(`pi -p`) suitable for non-interactive subprocess invocation.

JuliusCaesar already wraps native CLI tools as brains — Claude Code (`claude
-p`), Codex CLI (`codex exec -`), OpenCode (`opencode run`). pi.dev follows the
same execution model: invoke the native binary as a subprocess, feed the prompt
via stdin, capture stdout.

This spec defines the Python brain wrapper, the shell adapter, config wiring,
session capture strategy, tests, and operator-facing behavior needed for
`default_brain: pi` and `channels.<name>.brain: pi` to work in production.

## Verified facts (tested 2026-05-14 on macOS, pi v0.74.0)

These were tested before writing this revision. Do not treat as open questions.

1. **`pi -p` (no positional arg) reads stdin as the prompt.** No ARG_MAX
   limit. Preferred invocation pattern: pipe the prompt. Tested with:
   `echo "Say hello" | pi -p --no-context-files --no-extensions --no-skills --no-prompt-templates --no-themes`

2. **`pi -p` always writes a session file**, even with `--no-session`. Session
   files are JSONL in `~/.pi/agent/sessions/<cwd-slug>/` with format
   `<ISO-timestamp>_<uuid>.jsonl`. The UUID portion (after the `_`) is the
   session id for `--session`. Tested: pre/post snapshot works.

3. **`--session <uuid>` resumes correctly.** The UUID is the hex segment from
   the filename stem (e.g. `019e26ac-...` from
   `2026-05-14T13-28-21-813Z_019e26ac-...jsonl`). pi accepts both the full
   filename (without `.jsonl`) and just the UUID.

4. **cwd-slug formula:** `--` + `realpath(cwd).lstrip('/').replace('/', '-')`
   + `--`. pi resolves symlinks. Use `os.path.realpath()` in Python.

5. **pi's short model names (`sonnet`, `opus`) are ambiguous across
   providers.** On a machine with only DeepSeek configured, `--model sonnet`
   resolves to Amazon Bedrock (first alphabetically). Must always use fully
   qualified `provider/model` format (e.g. `anthropic/claude-sonnet-4-6`) for
   deterministic routing. The adapter script is responsible for mapping JC
   aliases to qualified pi model IDs.

## Non-goals

- Do not implement OAuth login for pi. `pi /login` remains operator-owned.
- Do not replace pi's tool system or session format. Invoke `pi` as-is and
  consume its stdout.
- Do not change the default instance brain away from `claude`.
- Do not require pi to be the operator's primary interactive tool. Gate pi
  behind the gateway's multi-brain router alongside other brains.
- Do not add pi-specific image or multimodal support. Flag as "no" until
  pi's non-interactive mode is proven to support image inputs.
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
| **Print mode** | `pi -p` — non-interactive, prints response to stdout and exits. Without positional arg, reads prompt from stdin. |
| **Model selection** | `pi --model provider/id` with optional `:<thinking>` suffix |
| **Thinking level** | `pi --thinking off\|minimal\|low\|medium\|high\|xhigh` |
| **Tools control** | `pi --no-tools` to disable all; `pi --tools <list>` to allowlist |
| **Session resume** | `pi --session <uuid>` to continue a session |
| **Context files** | Auto-loads `CLAUDE.md` / `AGENTS.md`; `--no-context-files` / `-nc` disables |
| **Prompt discovery off** | `--no-extensions`, `--no-skills`, `--no-prompt-templates`, and `--no-themes` disable operator-side prompt/code discovery |
| **System prompt** | `pi --append-system-prompt <text>` to append to system prompt |
| **Auth** | Subscription OAuth (`/login`) or API keys via environment variables |

## Current behavior

JuliusCaesar has seven brain implementations. No `pi` brain exists. A user
selecting `pi` as `default_brain` would get a config validation error
("unsupported brain").

## Desired behavior

### Brain lifecycle

1. **Register** `pi` in `SUPPORTED_BRAINS` and the `_BRAIN_REGISTRY`.
2. **Validate** `brains.pi.*` config overrides (bin, timeout_seconds, extra_args).
3. **Invoke** `pi -p` in print mode as a subprocess from the instance directory,
   feeding the full prompt via stdin.
4. **Capture** session id after invocation for conversation resume.
5. **Resume** via `pi --session <uuid> -p` on subsequent turns.

### Prompt delivery (stdin, no ARG_MAX cap)

`pi -p` without a positional argument reads the full prompt from stdin. No
ARG_MAX limit. This is the invocation pattern:

```bash
echo "$PROMPT" | pi -p \
   --no-context-files \
   --no-extensions \
   --no-skills \
   --no-prompt-templates \
   --no-themes \
   ${MODEL:+--model "$MODEL"} \
   ${SESSION:+--session "$SESSION"} \
   ${THINKING:+--thinking "$THINKING"} \
   --no-tools \
   [extra_args...]
```

No prompt truncation needed. Stdin can carry the full gateway preamble + user
message.

### Context files and L1 preamble

`needs_l1_preamble = True`. pi is invoked with `--no-context-files` to
suppress pi's own CLAUDE.md/AGENTS.md loading. The gateway preamble (built by
`Brain.prompt_for_event()`, in `lib/gateway/brains/base.py`) provides the full
L1 context, clock, metadata block, voice instructions, and known chats. This
keeps the prompt contract consistent with codex/opencode brains and avoids
edge cases from pi's CLAUDE.md parsing differing from JC's.

### Session capture

pi always writes session files under:

```
~/.pi/agent/sessions/<cwd-slug>/<ISO-timestamp>_<uuid>.jsonl
```

Capture uses the same pre/post snapshot pattern as `CodexBrain`
(`lib/gateway/brains/codex.py`):

1. `pre_invoke_snapshot()` snapshots `frozenset` of `*.jsonl` paths in the
   cwd-slug directory before invocation (`Brain.pre_invoke_snapshot()` in
   `lib/gateway/brains/base.py` stores the result in `self._pre_state`).
2. `capture_session_id()` diffs pre/post snapshots. If exactly one new file
   appeared, extracts the UUID from the stem (split on `_`, take second part,
   strip `.jsonl`).
3. If zero or multiple new files → return `None` → next turn falls back to
   transcript priming (already handled by `Brain.invoke()` for
   `needs_l1_preamble=True` brains).

**cwd-slug computation:**

```python
import os
def _pi_session_dir(cwd: Path) -> Path:
    real = os.path.realpath(str(cwd))
    slug = "--" + real.lstrip("/").replace("/", "-") + "--"
    return Path.home() / ".pi" / "agent" / "sessions" / slug
```

Resume passes the captured UUID to the adapter via `JC_RESUME_SESSION` env var,
restored from the conversation's saved session id in the gateway queue.

### Output contract injection

Override `prompt_for_event()` in `PiBrain` to append the gateway output
contract. The override delegates to `super().prompt_for_event(event)` for the
standard preamble, then appends the contract block.

```python
# lib/gateway/brains/pi.py

from .base import Brain

class PiBrain(Brain):
    name = "pi"
    needs_l1_preamble = True

    def prompt_for_event(self, event: Event) -> str:
        base = super().prompt_for_event(event)
        contract = """

[GATEWAY OUTPUT CONTRACT]

Your final stdout MUST be a single JSON object on a single line (no code
fences, no prose before or after) with exactly these fields:

  {"push_message_sent": <bool>, "message": <string>}

Rules:
- If you used PushNotification to deliver the user-facing output yourself,
  set push_message_sent=true. The 'message' field then becomes a short audit
  log of what you pushed — the framework will NOT re-deliver it.
- If you did NOT use PushNotification and want the framework to relay your
  reply to the user, set push_message_sent=false and put the full reply in
  'message'. The framework will deliver it to the channel.
- 'message' is always required. Use empty string only for genuine no-op
  silent runs.
- Emit ONLY the JSON object as your final output.
"""
        return base + contract

    # ... session capture, extra_env, extra_args below
```

`BrainOutput.parse_brain_output()` in `lib/gateway/brain_output.py` already
handles plain-text stdout as fallback and extracts embedded JSON contracts
from prose. No changes needed to the parser.

### Model mapping

JC brain specs use `<brain>:<model>` (e.g. `pi:sonnet`). pi's short model
names are provider-ambiguous, so the adapter must resolve JC aliases to fully
qualified `provider/model` IDs.

**Adapter model resolution table** (`pi.sh`):

| JC alias | pi `--model` value |
|----------|-------------------|
| (unset) | pi's default from `settings.json` |
| `sonnet` | `anthropic/claude-sonnet-4-6` |
| `opus` | `anthropic/claude-opus-4-7` |
| `haiku` | `anthropic/claude-haiku-4-5` |
| `gpt-5.4` | `openai/gpt-5.4` |
| `gpt-5.4-mini` | `openai/gpt-5.4-mini` |
| `gemini-2.5-pro` | `google/gemini-2.5-pro` |
| `gemini25` | `google/gemini-2.5-pro` |
| `gemini-2.0-flash` | `google/gemini-2.0-flash` |
| `gemini20` | `google/gemini-2.0-flash` |
| `provider/model` | passed through as-is |

**JC aliases** (`lib/gateway/brains/aliases.py`):

```python
"pi": "pi",
"pi-sonnet": "pi:sonnet",
"pi-opus": "pi:opus",
"pi-haiku": "pi:haiku",
"pi-gpt5": "pi:gpt-5.4",
"pi-mini": "pi:gpt-5.4-mini",
"pi-google": "pi:gemini-2.5-pro",
"pi-gemini": "pi:gemini-2.5-pro",
"pi-gemini25": "pi:gemini-2.5-pro",
"pi-gemini20": "pi:gemini-2.0-flash",
```

### Tools

pi ships with built-in tools: `read`, `bash`, `edit`, `write`, `grep`, `find`,
`ls`. For gateway chat, use `--no-tools` to disable all tools (read-only chat).
This is the safe default.

Brain override config:

```yaml
brains:
  pi:
    no_tools: true        # default: true. Set false for workers/coding.
    extra_args: []        # pass-through args for the pi CLI
```

The `no_tools` config key is a boolean. `PiBrain.extra_args_for_event()`
returns `("--no-tools",)` when `no_tools` is true. When false, pi defaults to
its full tool set.

Worker tool access: the `no_tools` override is per-brain, not per-event. If a
worker needs tools while the main chat doesn't, the operator sets
`no_tools: false` and relies on pi's `--tools <allowlist>` via `extra_args`, or
uses a different brain for workers. A per-invocation tools config is out of
scope for this spec.

### Operator discovery surfaces

`--no-extensions`, `--no-skills`, `--no-prompt-templates`, and `--no-themes`
are always passed to disable pi's TypeScript extension, skill, prompt
template, and theme discovery. This keeps gateway invocations deterministic.
Operators who want one of these surfaces can pass explicit override args via
`brains.pi.extra_args` (e.g. `-e ./my-ext.ts`), which appends after the fixed
args.

### Thinking level

pi supports `--thinking off|minimal|low|medium|high|xhigh`. The `Brains`
config carries a `thinking` key:

```yaml
brains:
  pi:
    thinking: "high"     # optional, defaults to unset (pi's default)
```

### Capability matrix

| Brain     | Text | Images | Tools | File edits | Gateway chat default |
|-----------|------|--------|-------|------------|----------------------|
| pi        | yes  | no     | yes   | yes        | yes, --no-tools      |

Image support: pi supports image pasting in interactive mode. Non-interactive
image support is unverified. Flag as "no" for v1. If pi's CLI later accepts
`--image` or `@file.png` in print mode, update the matrix with tests.

## Implementation plan

### Phase 1 — Shell adapter

**File:** `lib/heartbeat/adapters/pi.sh`

The adapter reads the prompt from stdin, builds the `pi -p` command line, and
writes stdout to the gateway. Model is `$1` (optional). Resume session id
comes from `$JC_RESUME_SESSION` (matches base `Brain.invoke()` env contract).

```bash
#!/usr/bin/env bash
# pi.dev adapter. Reads prompt from stdin, writes response to stdout.
# Model is $1 (optional).
set -euo pipefail
export PATH="...standard PATH..."

MODEL="${1:-}"

# Resolve JC model aliases to fully-qualified provider/model IDs.
# Strip "pi:" prefix first if present (worker path passes "pi:sonnet").
MODEL="${MODEL#pi:}"
case "$MODEL" in
    sonnet)               MODEL="anthropic/claude-sonnet-4-6" ;;
    opus)                 MODEL="anthropic/claude-opus-4-7"   ;;
    haiku|haiku-4-5*)     MODEL="anthropic/claude-haiku-4-5"  ;;
    gpt-5.4|gpt5)         MODEL="openai/gpt-5.4"             ;;
    gpt-5.4-mini|mini)    MODEL="openai/gpt-5.4-mini"        ;;
    gemini-2.5-pro)       MODEL="google/gemini-2.5-pro"      ;;
    "")                   ;;  # use pi's default model
    *)                    ;;  # pass through as-is (provider/model already)
esac

if ! command -v pi >/dev/null 2>&1; then
    echo "pi CLI not installed. See https://pi.dev" >&2
    exit 127
fi

ARGS=(
    "-p"
    "--no-context-files"
    "--no-extensions"
    "--no-skills"
    "--no-prompt-templates"
    "--no-themes"
)

# Tools: --no-tools is default for gateway chat.
# Allow override via JC_PI_NO_TOOLS=0.
if [[ "${JC_PI_NO_TOOLS:-1}" != "0" ]]; then
    ARGS+=("--no-tools")
fi

RESUME="${JC_RESUME_SESSION:-${WORKER_RESUME_SESSION:-}}"
if [[ -n "$RESUME" ]]; then
    ARGS+=("--session" "$RESUME")
fi

if [[ -n "$MODEL" ]]; then
    ARGS+=("--model" "$MODEL")
fi

# extra args from brain config (appended by Brain.invoke via extra_args_for_event)
if [[ $# -gt 1 ]]; then
    shift
    ARGS+=("$@")
fi

exec pi "${ARGS[@]}"
```

**Acceptance criteria** (test adapter behavior, not model behavior):

- `echo "hello" | bash pi.sh` → exits 0, stdout non-empty
- `echo "hello" | MODEL=sonnet bash pi.sh` → argv includes `--model anthropic/claude-sonnet-4-6`
- `echo "hello" | JC_RESUME_SESSION=abc123 bash pi.sh` → argv includes `--session abc123`
- `pi` not installed → exits 127 with stderr message
- Adapter path is executable

### Phase 2 — Python brain wrapper (includes session capture)

**File:** `lib/gateway/brains/pi.py`

```python
"""pi.dev brain wrapper."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..queue import Event
from .base import Brain

# pi session filenames: <ISO-timestamp>_<uuid>.jsonl
_PI_SESSION_FILE_RE = re.compile(r".*_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")


def _pi_session_dir(cwd: str) -> Path:
    """Return the pi session directory for the given cwd."""
    real = os.path.realpath(cwd)
    slug = "--" + real.lstrip("/").replace("/", "-") + "--"
    return Path.home() / ".pi" / "agent" / "sessions" / slug


def _snapshot_session_paths(root: Path) -> frozenset[str]:
    """Snapshot all session JSONL paths under root. Matches CodexBrain pattern."""
    if not root.is_dir():
        return frozenset()
    try:
        return frozenset(str(p) for p in root.rglob("*.jsonl"))
    except OSError:
        return frozenset()


class PiBrain(Brain):
    name = "pi"
    needs_l1_preamble = True

    # --- Brain override config keys consumed by this brain ---

    @property
    def _no_tools(self) -> bool:
        """Read no_tools from brain override config. Default: True."""
        raw = getattr(self.override, "no_tools", None)
        if raw is None:
            return True
        return bool(raw)

    @property
    def _thinking(self) -> str | None:
        """Read thinking level from brain override config. Default: None."""
        raw = getattr(self.override, "thinking", None)
        if raw and str(raw).strip():
            return str(raw).strip()
        return None

    # --- Subclass hooks ---

    def extra_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        # pi reads auth from ~/.pi/auth.json (OAuth) or env vars.
        # Inject API keys from instance .env so pi subprocess picks them up.
        # The gateway starts with env -i, so os.environ won't have them.
        from ..config import env_value
        for key_name in (
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
            "GOOGLE_API_KEY", "DEEPSEEK_API_KEY", "GROQ_API_KEY",
            "OPENROUTER_API_KEY",
        ):
            key_value = env_value(self.instance_dir, key_name)
            if key_value:
                env[key_name] = key_value
        # Signal the adapter to enable/disable tools.
        env["JC_PI_NO_TOOLS"] = "1" if self._no_tools else "0"
        return env

    def extra_args_for_event(self, event: Event) -> tuple[str, ...]:
        args: list[str] = []
        thinking = self._thinking
        if thinking:
            args.extend(["--thinking", thinking])
        return tuple(args)

    def prompt_for_event(self, event: Event) -> str:
        """Build full prompt with output contract appended."""
        base = super().prompt_for_event(event)
        contract = """

[GATEWAY OUTPUT CONTRACT]

Your final stdout MUST be a single JSON object on a single line (no code
fences, no prose before or after) with exactly these fields:

  {"push_message_sent": <bool>, "message": <string>}

Rules:
- push_message_sent=true if you delivered output yourself via PushNotification;
  'message' is then an audit log. The framework will NOT re-deliver.
- push_message_sent=false if the framework should deliver 'message' to the channel.
- 'message' is always required. Empty string only for no-op silent runs.
- Emit ONLY the JSON object as your final output.
"""
        return base + contract

    # --- Session capture (matches CodexBrain pattern) ---

    def pre_invoke_snapshot(self) -> frozenset[str]:
        """Snapshot session JSONL paths before invocation.

        Referenced by Brain.invoke() in base.py: the return value is stored
        on self._pre_state and passed to capture_session_id via the
        started_at / self._pre_state convention.
        """
        return _snapshot_session_paths(
            _pi_session_dir(str(self.instance_dir))
        )

    def capture_session_id(self, started_at: str) -> str | None:
        """Return the session UUID created by this invocation, or None.

        Uses pre/post snapshot of the pi session directory. Returns None when:
        - No new session file was created (adapter failed or pi didn't write).
        - More than one new file appeared (concurrent pi activity).
        - The new filename doesn't match the expected <ts>_<uuid>.jsonl pattern.
        """
        before = getattr(self, "_pre_state", None)
        if not isinstance(before, frozenset):
            before = frozenset()
        after = _snapshot_session_paths(
            _pi_session_dir(str(self.instance_dir))
        )
        new_paths = after - before
        if not new_paths or len(new_paths) > 1:
            return None
        stem = Path(next(iter(new_paths))).stem
        match = _PI_SESSION_FILE_RE.match(stem)
        return match.group(1) if match else None
```

**Acceptance:**

```bash
pytest tests/gateway/test_pi_brain.py
```

Required test cases:
- `PiBrain.name == "pi"`
- `PiBrain.needs_l1_preamble == True`
- `pre_invoke_snapshot` returns `frozenset` of paths; empty when dir missing
- `capture_session_id` returns UUID from `<ts>_<uuid>.jsonl` filename
- `capture_session_id` returns `None` when no new file appears
- `capture_session_id` returns `None` when multiple new files appear
- `capture_session_id` returns `None` when filename doesn't match pattern
- `extra_env` injects API keys from instance `.env`
- `extra_env` sets `JC_PI_NO_TOOLS=1` by default
- `extra_args_for_event` returns `--thinking high` when config set
- `prompt_for_event` output contains the gateway contract block
- `_pi_session_dir` produces correct slug from instance path

### Phase 3 — Registration and config

**Files:**
- `lib/gateway/brains/__init__.py` — add `PiBrain` to exports
- `lib/gateway/brains/dispatch.py` — add `"pi": PiBrain` to `_BRAIN_REGISTRY`
- `lib/gateway/config.py` — add `"pi"` to `SUPPORTED_BRAINS` and
  `SUPPORTED_UNSAFE_FALLBACK_BRAINS`. Add `no_tools` and `thinking` to
  `BrainOverrideConfig` fields and validation.
- `lib/gateway/brains/aliases.py` — add pi aliases
- `lib/gateway/capabilities.py` — add pi to capability matrix (text: yes,
  images: no, tools: yes, file_edits: yes)

**BrainOverrideConfig additions:**

```python
@dataclass(frozen=True)
class BrainOverrideConfig:
    bin: str | None = None
    sandbox: str | None = None
    yolo: bool | None = None
    timeout_seconds: int | None = None
    extra_args: tuple[str, ...] = ()
    no_tools: bool | None = None      # new: pi only (ignored by other brains)
    thinking: str | None = None       # new: pi only (ignored by other brains)
```

`brains.pi.no_tools` and `brains.pi.thinking` are validated but only consumed
by `PiBrain`. Other brains ignore them silently (fields are optional, default
`None`).

**Acceptance:**

```bash
pytest tests/gateway/test_brain_specs.py tests/gateway/test_config_env.py
```

Required test cases:
- `default_brain: pi` passes config validation
- `channels.telegram.brain: pi` passes config validation
- `default_brain: pi:sonnet` preserves model
- `/brain pi-sonnet` resolves to `pi:sonnet`
- `/brain pi-google` resolves to `pi:gemini-2.5-pro`
- `pi` appears in `supported_brains()` output
- `brains.pi.no_tools: false` loads correctly
- `brains.pi.thinking: high` loads correctly

### Phase 4 — End-to-end integration

**Files:** No new files. Integration across existing suite.

Work:
1. Manual smoke test with `default_brain: pi`.
2. Test conversation continuity across multiple turns (session capture +
   resume).
3. Test that `brains.pi.*` overrides work (custom bin, no_tools, thinking,
   extra_args).
4. Test `jc doctor` reports pi availability and configuration.

**Acceptance:**

```bash
# Manual smoke
jc doctor                          # reports pi CLI installed/version + config
jc gateway enqueue --source telegram --conversation-id pi-smoke --content "hello"
jc gateway work-once               # pi invoked, response delivered

# Multi-turn
jc gateway enqueue --source telegram --conversation-id pi-smoke --content "what did I just say?"
jc gateway work-once               # pi resumes session, recalls "hello"
```

## Rollout plan

1. Land Phase 1 (adapter) + Phase 2 (wrapper with session capture) together.
   These form a complete, testable brain.
2. Land Phase 3 (registration + config) before merging to `main`.
3. Phase 4 (integration) is the merge-gate manual smoke.

## Backward compatibility

- Existing brain configs are unaffected.
- `pi` is new; no existing instances use it.
- `BrainOverrideConfig` gains two optional fields (`no_tools`, `thinking`)
  with `None` defaults. Existing code that constructs `BrainOverrideConfig`
  without these fields continues to work.
- No migration needed for existing instances.

## Security and safety

- pi invoked via `-p` (print mode) — never an interactive TUI session.
- Gateway chat defaults to `--no-tools` (`JC_PI_NO_TOOLS=1` via env var).
  Operators override via `brains.pi.no_tools: false`.
- **Never pass API keys on the command line.** pi accepts `--api-key` but
  this exposes the key in `ps` output. All credentials are passed via
  environment variables only (`extra_env()` injects keys from instance
  `.env` into the subprocess environment). The adapter script does not
  accept or forward `--api-key`.
- pi's auth state (OAuth tokens in `~/.pi/auth.json`, API keys in env)
  must be readable by the gateway process user.
- `--no-extensions`, `--no-skills`, `--no-prompt-templates`, and
  `--no-themes` are always passed to prevent arbitrary operator prompt/code
  discovery from changing gateway context.
- If `brains.pi.bin` is set to a custom path, `Brain.validate()`
  (inherited from base) checks it's executable at invocation time.

## Open questions

1. **Thinking level config:** should `brains.pi.thinking` be a per-brain
   config key or a future per-message concept? Current spec makes it
   per-brain. Revisit if per-message control is needed.
2. **Worker tool override:** current config is per-brain. If an operator
   wants `--no-tools` for chat but full tools for workers, they must use a
   different brain for workers or set `no_tools: false` and use
   `--tools <allowlist>` via `extra_args`. A per-invocation tools config
   is future work.
3. **pi `--mode json`:** pi supports JSONL output mode. This could
   potentially replace the JSON output contract and give structured access
   to tool calls. Not needed for v1; evaluate if plain `-p` stdout proves
   insufficient.
4. **Provider selection:** the spec maps JC model aliases to specific
   providers (Anthropic for Claude models, OpenAI for GPT models). If an
   operator wants the same model name on a different provider, they use
   `pi:provider/model` directly (adapter passes through as-is). Is a
   `brains.pi.provider` config key warranted?
5. **Session directory override:** pi supports `--session-dir`. If an
   operator uses a custom session directory (via pi settings or env), the
   `_pi_session_dir()` computation will miss sessions. Add a config key
   or detect from pi's settings.json? Out of scope for v1 — document as
   known limitation.

## Definition of done

pi is production-ready as a gateway brain when:

- `default_brain: pi` works and routes to `pi -p` with stdin prompt.
- `channels.telegram.brain: pi` works.
- `channels.telegram.brain: pi:sonnet` preserves model, resolves to
  `anthropic/claude-sonnet-4-6`.
- pi receives full L1 preamble via gateway (not double-loaded from CLAUDE.md).
- Session capture returns correct UUID or `None` safely; multi-turn
  conversation continuity works (either native resume or transcript priming).
- `brains.pi` config overrides (bin, no_tools, thinking, extra_args) work.
- `--no-context-files`, `--no-extensions`, `--no-skills`,
  `--no-prompt-templates`, and `--no-themes` are always passed.
- `--no-tools` is default for gateway chat; `JC_PI_NO_TOOLS=1` env var
  controls it.
- Output contract injection works; `BrainOutput` parser handles pi's stdout.
- `jc doctor` reports pi CLI presence, version, and config.
- Alias `/brain pi-sonnet` resolves correctly.
- **Never** passes API keys on the command line.
- Targeted tests are green:

```bash
pytest \
  tests/gateway/test_pi_brain.py \
  tests/gateway/test_brain_specs.py \
  tests/gateway/test_brain_output.py \
  tests/gateway/test_transcripts_runtime.py \
  tests/gateway/test_config_env.py
```
