# Spec: Codex as main brain hardening

**Status:** Draft
**Date:** 2026-05-01
**Branch base:** `main`
**Owner:** Rachel / gateway

## Goal

Make Codex reliable as a first-class gateway main brain, not only as a worker
or triage tool.

Today `default_brain: codex` and `channels.<name>.brain: codex` are wired
enough to launch `codex exec`, but several contracts are incomplete:

1. Brain specs with models are validated but then misloaded.
2. `codex_api` main chat loses conversation continuity.
3. `codex` CLI session capture can save an unrelated global session id.
4. Codex does not receive the same instance instructions Claude receives via
   `CLAUDE.md`.
5. The instance `.codex/` template is not guaranteed to be used by gateway
   Codex calls.
6. `brains.codex.yolo` is parsed but ignored.
7. `gpt5` aliases point at a stale / likely invalid Codex model.
8. Telegram image events are forced away from Codex even though current Codex
   supports image input.

This spec defines the fixes, tests, and operator-facing behavior needed before
Codex can be recommended as the default brain for a production instance.

## Non-goals

- Do not remove Claude support.
- Do not change the default instance brain away from `claude`.
- Do not make `codex_api` a replacement for the Codex CLI tool loop. The API
  path is for low-latency chat/triage, not file-editing agent work.
- Do not implement OAuth login. `codex login` remains owned by the Codex CLI.
- Do not require public OpenAI API keys. The existing `codex_auth` subscription
  token path remains the direct-API mechanism.

## Current behavior

### Shell Codex brain

`lib/gateway/brains/codex.py` wraps `lib/heartbeat/adapters/codex.sh`.
The adapter runs:

```bash
codex exec [sandbox flags] [--model <model>] -
codex exec resume <session> [sandbox flags] [--model <model>] -
```

`Brain.invoke()` sends the full prompt on stdin, captures stdout as the final
response, and stores a native session id if the wrapper can find one.

### Direct API Codex brain

`codex_api` bypasses the CLI and calls the ChatGPT Codex backend through
`lib/codex_auth`. It is useful for triage and potentially for low-latency chat,
but it has no shell tools and no native session persistence.

### Context loading

Claude Code auto-loads `<instance>/CLAUDE.md`. Non-Claude brains receive
`lib/gateway/context.py:render_preamble()`, which currently concatenates only:

- `memory/L1/IDENTITY.md`
- `memory/L1/USER.md`
- `memory/L1/RULES.md`
- `memory/L1/HOT.md`

That is not equivalent to `CLAUDE.md`, which also imports `CHATS.md` and carries
operator-facing instructions such as L2 search guidance, framework commands, and
token-efficiency rules.

## Desired behavior

### Brain specs

Every brain spec must have one canonical parser:

```python
BrainSpec(brain="codex", model="gpt-5.4-mini")
```

Valid inputs:

- `codex`
- `codex:gpt-5.4-mini`
- `codex_api:gpt-5.4-mini`
- `claude:opus-4-7-1m`
- channel-level `brain: codex` plus `model: gpt-5.4-mini`

Rules:

- `default_brain` may be either a bare brain or `<brain>:<model>`.
- `channels.<name>.brain` may be either a bare brain or `<brain>:<model>`.
- If both `channels.<name>.brain` includes a model and `channels.<name>.model`
  is also set, fail validation with a clear error. Do not silently pick one.
- Unsupported brain names are config errors. Do not silently fall back to
  `claude`.
- Unknown model names may be warnings rather than hard errors because model
  catalogs rotate. For Codex aliases shipped by this repo, use known-good
  current defaults.

### Codex context parity

Codex must receive a semantically equivalent version of the instance instructions
that Claude receives through `CLAUDE.md`.

Implementation should not depend on Codex auto-loading `CLAUDE.md`, because
that file is Claude-specific and uses Claude import syntax. Instead, build a
gateway-native preamble with these sections:

1. Instance role and operating contract.
2. Expanded L1 memory:
   - `IDENTITY.md`
   - `USER.md`
   - `RULES.md`
   - `HOT.md`
   - `CHATS.md`
3. L2 retrieval instructions:
   - `jc memory search "<query>"`
   - `jc memory read <slug>`
   - `jc transcripts ...` commands from `RULES.md`
4. Framework command hints from the instance template.
5. Token-efficiency / caveman rules from `CLAUDE.md`, unless an instance
   explicitly opts out.

The preamble renderer should be shared by all non-Claude brains, but it must be
Codex-aware where useful:

- Tell Codex it is running as a gateway chat brain, not as an autonomous worker.
- Instruct Codex not to edit files unless the user explicitly asks for coding or
  maintenance work.
- Instruct Codex to answer the user, not to narrate internal gateway metadata.

### Instance `.codex/`

Instances already contain:

```text
<instance>/.codex/config.toml
<instance>/.codex/hooks.json
```

Gateway Codex calls must either:

1. Set `CODEX_HOME=<instance>/.codex` while preserving access to the operator's
   real auth, or
2. Stop shipping an instance `.codex/` template and move those settings into the
   explicit gateway preamble/config.

Preferred path:

- Do not set `CODEX_HOME` blindly, because that may make Codex look for
  `<instance>/.codex/auth.json` instead of `~/.codex/auth.json`.
- Convert the instance hooks/config intent into explicit adapter flags and
  prompt content.
- Add a `jc doctor` warning if `<instance>/.codex/` exists but is not used by
  the current Codex runtime path.

If future Codex supports separate config-home and auth-home, revisit this and
wire the instance config directly.

### Codex sandbox / yolo

`brains.codex.yolo: true` must work.

Resolution order:

1. `brains.codex.yolo: true` -> `CODEX_SANDBOX=yolo`
2. `brains.codex.sandbox: <value>` -> `CODEX_SANDBOX=<value>`
3. unset -> default adapter sandbox

If both `yolo: true` and `sandbox` are set to a non-yolo value, config
validation should fail. Safety settings must not be ambiguous.

Recommended defaults:

- Gateway main chat: `read-only`
- Worker / coding tasks: `workspace-write`
- Explicit maintenance task with operator consent: `yolo`

The existing adapter default of `workspace-write` is too permissive for normal
chat if Codex is the main brain.

### Codex model aliases

Replace stale aliases:

```python
"gpt5": "codex:gpt-5"
"gpt-5": "codex:gpt-5"
```

with current Codex catalog defaults:

```python
"gpt5": "codex:gpt-5.4"
"gpt-5": "codex:gpt-5.4"
"gpt54": "codex:gpt-5.4"
"mini": "codex:gpt-5.4-mini"
"codex-mini": "codex:gpt-5.4-mini"
"codex-coding": "codex:gpt-5.3-codex"
```

This is based on the local Codex model cache observed on 2026-05-01:

- `gpt-5.5`
- `gpt-5.4`
- `gpt-5.4-mini`
- `gpt-5.3-codex`
- `gpt-5.2`
- `codex-auto-review`

Model catalog can rotate, so aliases should be tested against syntax and
documented, not treated as a permanent API guarantee.

### Conversation continuity

#### `codex` CLI

Do not capture the newest global JSONL by timestamp alone.

Acceptable options:

1. Use a pre/post snapshot of `~/.codex/session_index.jsonl` or `~/.codex/sessions`
   and select a session created by this invocation.
2. Prefer a Codex CLI-supported `--output-last-message` or future
   machine-readable session metadata if available.
3. If no reliable session id can be captured, do not save one. Fall back to
   transcript priming on the next turn instead of risking a wrong resume.

Hard requirement:

- Never resume a session id that cannot be tied to the same gateway invocation
  or same instance conversation.

#### `codex_api`

`codex_api` is stateless. It must receive transcript priming whenever
`event.conversation_id` exists and prior transcript lines exist.

Implementation options:

- Factor transcript priming out of `Brain.invoke()` into a helper that both
  `Brain.invoke()` and `CodexApiBrain.invoke()` call.
- Or call `_build_transcript_priming(event)` from `CodexApiBrain.invoke()` before
  `adapter.run()`.

Do not require a `resume_session` for `codex_api`; it will always be `None`.

### Images and multimodal input

Codex currently supports `--image <FILE>` and local model metadata reports image
input support. Codex should not be forced to Claude/Gemini for every Telegram
image event.

Rules:

- If selected brain is `codex` and `meta.image_path` exists, pass one or more
  `--image <path>` flags to `codex exec`.
- If selected brain is `codex_api`, do not claim image support until the direct
  Responses payload supports image parts and has tests.
- The runtime vision fallback should consult a capability matrix rather than a
  hardcoded `brain not in ("claude", "gemini")`.

Capability matrix v1:

| Brain     | Text | Images | Tools | File edits | Gateway chat default |
|-----------|------|--------|-------|------------|----------------------|
| claude    | yes  | yes    | yes   | yes        | yes                  |
| codex     | yes  | yes    | yes   | yes        | yes, read-only       |
| codex_api | yes  | no     | no    | no         | yes, after priming   |
| gemini    | yes  | yes    | partial | partial  | yes                  |
| opencode  | yes  | no     | yes   | yes        | code only            |
| aider     | yes  | no     | git   | yes        | code only            |

## Implementation plan

### Phase 1 — Config correctness

Files:

- `lib/gateway/config.py`
- `lib/gateway/router.py`
- `lib/gateway/brains/aliases.py`
- `tests/gateway/test_config.py` or new `tests/gateway/test_brain_specs.py`
- `tests/gateway/test_router.py`

Work:

1. Add `BrainSpec` parser and formatter.
2. Make `GatewayConfig` store `default_brain` and `default_model` correctly
   when `default_brain` includes a model.
3. Make channel configs preserve model from `brain: codex:gpt-5.4-mini`.
4. Fail validation on ambiguous duplicated model specs.
5. Update aliases to current Codex catalog.

Acceptance:

```bash
pytest tests/gateway/test_brain_specs.py tests/gateway/test_router.py
```

Required test cases:

- `default_brain: codex:gpt-5.4-mini` routes to `brain=codex`,
  `model=gpt-5.4-mini`.
- `channels.telegram.brain: codex:gpt-5.4-mini` routes with the model preserved.
- `channels.telegram.brain: codex:gpt-5.4-mini` plus
  `channels.telegram.model: gpt-5.5` fails config validation.
- `default_brain: bogus` fails config validation.
- `/brain gpt5` resolves to a current Codex model.

### Phase 2 — Context parity

Files:

- `lib/gateway/context.py`
- `templates/init-instance/CLAUDE.md`
- `templates/init-instance/memory/L1/RULES.md`
- `tests/gateway/test_context.py`

Work:

1. Add `CHATS.md` to `L1_FILES`.
2. Add a Codex/non-Claude preamble section with the useful instructions that
   currently live only in `CLAUDE.md`.
3. Keep cache invalidation based on all included files.
4. Make context rendering resilient when `CHATS.md` is missing in older
   instances.

Acceptance:

```bash
pytest tests/gateway/test_context.py tests/gateway/test_chat_preamble.py
```

Required test cases:

- Preamble includes `CHATS.md` when present.
- Preamble includes L2 memory command guidance.
- Preamble includes token-efficiency instructions from the template contract.
- Updating `CHATS.md` invalidates the context cache.

### Phase 3 — Codex adapter flags and sandbox

Files:

- `lib/gateway/brains/codex.py`
- `lib/heartbeat/adapters/codex.sh`
- `lib/gateway/config.py`
- `tests/gateway/test_codex_brain.py`

Work:

1. Map `BrainOverrideConfig.yolo` to `CODEX_SANDBOX=yolo`.
2. Add validation for `yolo` + conflicting `sandbox`.
3. Make the default gateway Codex sandbox configurable and document recommended
   `read-only`.
4. Pass image paths as `--image` flags when present in event metadata.
5. Pass `--ask-for-approval never` for non-interactive gateway calls, unless
   Codex CLI already defaults to non-interactive never-approval mode. Verify
   against the installed CLI before implementing.

Acceptance:

```bash
pytest tests/gateway/test_codex_brain.py
```

Required test cases:

- `brains.codex.yolo: true` produces the dangerous bypass flag.
- `brains.codex.sandbox: read-only` produces read-only sandbox args.
- Invalid sandbox value fails before adapter execution.
- `image_path` metadata adds `--image <path>`.
- Adapter argv matches current `codex exec --help`.

### Phase 4 — Session safety

Files:

- `lib/gateway/brains/codex.py`
- `lib/gateway/brains/base.py`
- `tests/gateway/test_codex_sessions.py`

Work:

1. Replace timestamp-only global scan with a pre/post snapshot or session-index
   aware capture.
2. If no safe session id is found, return `None`.
3. Ensure transcript priming still gives continuity when native Codex resume is
   unavailable.
4. Add logging when Codex session capture is skipped for safety.

Acceptance:

```bash
pytest tests/gateway/test_codex_sessions.py tests/gateway/test_transcripts_runtime.py
```

Required test cases:

- Concurrent unrelated Codex JSONL created after gateway start is not captured.
- A session created by the gateway invocation is captured.
- No session id -> next turn receives transcript priming.
- Existing saved session id is resumed only when Codex accepts it.

### Phase 5 — `codex_api` main chat continuity

Files:

- `lib/gateway/brains/codex_api.py`
- `lib/gateway/brains/base.py`
- `tests/gateway/test_transcripts_runtime.py`
- `tests/codex_auth/test_responses.py`

Work:

1. Share transcript priming logic with the direct API wrapper.
2. Keep `codex_api` stateless; do not invent a fake session id.
3. Add a short system instruction for direct API chat:
   - answer as the instance assistant
   - use transcript priming for continuity
   - do not expose metadata unless useful

Acceptance:

```bash
pytest tests/gateway/test_transcripts_runtime.py tests/codex_auth/test_responses.py
```

Required test cases:

- Second `codex_api` turn includes previous assistant/user transcript lines.
- Just-enqueued current user message is not duplicated in the priming block.
- Empty transcript does not add a useless priming header.

### Phase 6 — Doctor and docs

Files:

- `bin/jc-doctor`
- `docs/MIGRATION-0.2-to-0.3.md`
- `docs/kb/contract/brain-capabilities.md`
- `docs/kb/contract/adapter-and-delivery-contracts.md`
- this spec

Work:

1. `jc doctor` should report:
   - Codex CLI installed and version.
   - Codex login/auth state present.
   - Current model aliases.
   - Whether instance `.codex/` is active, ignored, or intentionally unused.
   - Warning when `default_brain: codex` uses write-capable sandbox for normal
     chat.
2. Update migration docs to recommend:
   - `default_brain: codex:gpt-5.4-mini` for low-cost testing.
   - `brains.codex.sandbox: read-only` for chat.
   - `codex_api:gpt-5.4-mini` only after transcript priming is fixed.

Acceptance:

```bash
pytest tests/test_doctor.py tests/gateway/test_channels.py
```

Manual smoke:

```bash
jc doctor
jc gateway enqueue --source telegram --conversation-id codex-smoke --content "hello"
jc gateway work-once
```

## Rollout plan

1. Land Phase 1 first. Config correctness blocks every other behavior.
2. Land Phase 2 before recommending Codex as a chat brain.
3. Land Phase 3 before using Codex on image/coding mixed conversations.
4. Land Phase 4 before enabling native Codex session resume in production.
5. Land Phase 5 before using `codex_api` for anything beyond triage.
6. Update `jc setup` and docs only after tests cover the full path.

## Backward compatibility

- Existing `default_brain: codex` remains valid.
- Existing `channels.telegram.brain: codex` remains valid.
- Existing `default_model` remains valid.
- Existing `brains.codex.sandbox` remains valid.
- Existing `brains.codex.yolo` starts working; if combined with conflicting
  sandbox values, validation becomes stricter by design.
- Existing Claude instances are unaffected except for shared parser validation.

## Security and safety

- Main chat should not default to write-capable Codex sandbox.
- Direct API path must never log bearer tokens.
- Codex CLI path must never resume an untrusted unrelated session.
- Image paths passed to Codex must be local files created by gateway media
  ingestion, not arbitrary user-provided paths.
- Config validation should fail closed. Silent fallback to Claude is not
  acceptable for brain specs because it hides operator intent.

## Open questions

1. Should the long-term recommended main brain be `codex` CLI or `codex_api`?
   CLI has tools/session behavior; API has latency/cost advantages.
2. Should `.codex/` stay in instance templates if gateway never sets
   `CODEX_HOME`?
3. Should Codex model aliases be generated from `~/.codex/models_cache.json` at
   runtime, or kept static for reproducibility?
4. Should gateway chat ever allow write-capable Codex by default, or require an
   explicit `[codex-write]` / worker handoff path?
5. Should transcript priming be applied to all non-Claude brains even when a
   native resume id exists, or only when resume is missing?

## Definition of done

Codex is production-ready as a main brain when all are true:

- `default_brain: codex:gpt-5.4-mini` works and preserves the model.
- `channels.telegram.brain: codex:gpt-5.4-mini` works and preserves the model.
- Codex receives L1, `CHATS.md`, L2 retrieval instructions, transcript guidance,
  and token-efficiency rules.
- `brains.codex.yolo` and `brains.codex.sandbox` behave predictably.
- Codex session capture cannot pick unrelated sessions.
- `codex_api` gets transcript priming.
- Telegram image events can stay on Codex CLI when Codex is selected.
- `jc doctor` clearly reports Codex readiness and risky sandbox settings.
- Targeted tests are green:

```bash
pytest \
  tests/gateway/test_brain_specs.py \
  tests/gateway/test_context.py \
  tests/gateway/test_codex_brain.py \
  tests/gateway/test_codex_sessions.py \
  tests/gateway/test_transcripts_runtime.py \
  tests/gateway/test_router.py \
  tests/codex_auth/test_responses.py
```
