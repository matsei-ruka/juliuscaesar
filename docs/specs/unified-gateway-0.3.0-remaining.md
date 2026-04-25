# Spec: Unified Gateway 0.3.0 — Remaining Work

**Status:** Draft — supersedes the unshipped portions of `unified-gateway-0.3.0.md`
**Date:** 2026-04-25
**Branch base:** `main` (gateway foundation already merged via `7d7a1ad`, `77dbdcf`)

---

## 1. Scope

This spec covers everything from the original 0.3.0 unified-gateway design that is **not yet on main**. The foundation (queue, runtime, Telegram + Slack channels, watchdog gateway mode, doctor section, claude brain via heartbeat shell adapters) shipped. The rest did not.

Naming may diverge from the original spec where the implementation already chose a different name (e.g. `RUNTIME_MODE` instead of `BRAIN_RUNTIME`, `legacy-claude` instead of `mcp-plugin`). Pick the cleanest name at implementation time; do not churn existing names just to match the old draft.

### 1.1 Drops vs original spec

- **Web channel (Bun localhost UI) — REMOVED.** Not shipping in 0.3.0. No `lib/gateway/channels/web.py`, no `external_plugins/fakechat` integration, no `web:` config block. If a debugging UI is needed during development, use `jc gateway enqueue` from the CLI.
- Web-based integration tests in Sprint 5 are replaced by a `jc gateway enqueue` smoke test that uses a fake brain adapter.

### 1.2 Keeps from original spec

- Triage layer with all three backends (ollama, openrouter, claude-channel).
- Multi-brain Python adapters wrapping the existing shell adapters.
- Discord, voice, jc-events, cron channels.
- Sticky brain + idle timeout + confidence threshold + fallback.
- Brain override syntax (`[opus]` prefix and `/brain X` slash command).
- `bin/jc-migrate-to-0.3` migration tool.
- Structured JSON logs, backpressure, log rotation, timeout enforcement.
- Security audit via worker spawn (Gemini bug-hunt + Opus injection audit).
- KB / ADR / migration docs.
- `v0.3.0` tag.

---

## 2. Current state (as of 2026-04-25)

What exists on main:

```
lib/gateway/
  __init__.py
  queue.py        # SQLite queue, sessions table embedded, dedup, retries, leases
  runtime.py      # GatewayRuntime dispatcher loop; routing inlined via config.brain_for(channel)
  channels.py     # Telegram long-poll + Slack Socket Mode in one module
  brain.py        # Single dispatch into lib/heartbeat/adapters/<tool>.sh
  config.py       # ops/gateway.yaml loader, ChannelConfig, GatewayConfig
bin/jc-gateway    # init/start/stop/restart/run/status/tail/logs/enqueue/claim/complete/fail/list/events/retry/config/work-once
lib/watchdog/watchdog.sh  # main_gateway() supervises gateway daemon
templates/init-instance/ops/watchdog.conf  # RUNTIME_MODE=gateway default
docs/kb/subsystem/gateway-queue.md
docs/kb/subsystem/watchdog-runtime.md
docs/kb/contract/adapter-and-delivery-contracts.md
docs/kb/decision/native-cli-over-api-simulation.md
```

What is missing: per-channel module split, separate router and sessions modules, multi-brain Python wrappers, context loader, brain override syntax, triage in any form, Discord / jc-events / cron / voice channels, migration tooling, hardening (backpressure, JSON logs, rotation, timeout enforcement), tests, release docs, ADR, brain capability contract, `v0.3.0` tag.

---

## 3. Sprint Plan

Five sprints. Each depends on the previous. Each ends with a green `jc doctor` and a working e2e smoke.

### Sprint 1 — Internal refactor for extensibility

**Goal:** Move from the monolithic `channels.py` / `brain.py` / inlined-router shape to the per-channel / per-brain / pure-router shape that triage and multi-brain need to land cleanly. No user-visible change.

**Deliverables**

1. **Channels split.** Replace `lib/gateway/channels.py` with `lib/gateway/channels/__init__.py` plus:
   - `lib/gateway/channels/base.py` — `Channel` ABC with `inbound(emit)`, `outbound(event, response)`, `NAME`, `DIRECTION`, `CONFIG_KEYS`.
   - `lib/gateway/channels/telegram.py` — current Telegram code lifted from `channels.py`.
   - `lib/gateway/channels/slack.py` — current Slack Socket Mode code lifted from `channels.py`.
   - `lib/gateway/channels/registry.py` — discovery / load order / enabled-flag gating.
2. **Sessions split.** Extract the session table and helpers from `lib/gateway/queue.py` into `lib/gateway/sessions.py`. Schema unchanged. Queue continues to read/write through the new module. Add `get_active(channel, user_id) -> Optional[BrainSelection]` and `record_response(channel, user_id, brain, session_id)` wrappers used in Sprint 4 by triage.
3. **Router module.** New `lib/gateway/router.py` that exposes a pure function:
   ```python
   def route(event: Event, sticky: Optional[BrainSelection], triage: Optional[TriageResult], cfg: GatewayConfig) -> BrainSelection
   ```
   For Sprint 1 it returns `cfg.brain_for(event.source)`. Triage and sticky path lands in Sprint 4 inside the same function — runtime never grows new routing branches.
4. **Runtime cleanup.** `lib/gateway/runtime.py` calls `router.route(...)` instead of `config.brain_for(channel)` directly. Brain invocation goes through `lib/gateway/brains/dispatch.py` (added in Sprint 3) — for now, keep `brain.py` as the only call site, but rename its public function to `invoke_brain(...)` so Sprint 3 can swap implementations without touching runtime.
5. **Tests.** Unit test `router.route` covers: explicit `event.meta.brain_override` (added in Sprint 3) → wins; sticky present → wins; nothing → `cfg.brain_for(channel)`. Even though the override and sticky branches are stubs in Sprint 1, the contract is tested.

**Definition of done**

- `pytest tests/gateway/test_router.py` green.
- Existing `jc gateway work-once` smoke still passes.
- `jc gateway start` on the production instance shows zero behavioral change.
- Diff is purely structural; `git log --stat` shows new files + small runtime edit.

**Out of scope:** New channels, new brains, triage, override syntax.

---

### Sprint 2 — System events, cron, Discord, voice

**Goal:** All planned input/output channels except web. Worker → auto-synthesis pipeline closed. Heartbeat tasks flow through the queue.

**Deliverables**

1. **`lib/gateway/channels/jc_events.py`.** Watches `<instance>/state/events/*.json` (poll-based fallback if `inotify` unavailable; macOS dev needs poll). Event JSON contract:
   ```json
   {
     "event_type": "worker.completed",
     "worker_id": 18,
     "topic": "fix bugs",
     "status": "done",
     "duration_seconds": 145,
     "result_path": "state/workers/18/result",
     "notify_chat_id": "28547271",
     "notify_channel": "telegram"
   }
   ```
   Channel emits an Event with `source="jc-events"`, `content` = a synthesis prompt referencing the result path, `meta.delivery_channel = notify_channel`, `meta.delivery_user_id = notify_chat_id`. File is deleted after enqueue. `notify_channel` is what tells outbound where to deliver.
2. **`bin/jc-workers` rewrite of completion notification.** Replace the current direct Telegram send with a write to `state/events/<id>-completed.json`. Keep `WORKERS_NOTIFY_MODE=telegram-direct` env flag for forced backwards compatibility (single-line override; default = events).
3. **`lib/gateway/channels/cron.py`.** Replaces direct heartbeat-runner-to-adapter wiring for instances where `RUNTIME_MODE=gateway`. The cron channel reads `heartbeat/tasks.yaml`, computes the same hash-delta, and on delta enqueues an event with `source="cron"`, `meta.task_name`, `meta.brain_override` (the task's pinned brain). The router honors `meta.brain_override` (Sprint 3 wires this) so cron tasks bypass triage. `jc heartbeat run <task>` standalone path is preserved (no breakage outside gateway mode).
4. **`lib/gateway/channels/discord.py`.** `discord.py` library, gateway intent, DMs + mentions inbound, thread reply outbound. Token from `DISCORD_BOT_TOKEN`. Optional dependency (gated import; doctor reports "missing `discord.py` package" if enabled but unimportable).
5. **`lib/gateway/channels/voice.py`.** Pairs with another I/O channel via `paired_with: telegram` (or slack/discord). Inbound: receives audio file references from the paired channel's media attachments, transcribes via `lib/voice/dashscope_asr.py` (existing), enqueues a text event with `meta.voice = {"asr_text": "...", "audio_path": "..."}`. Outbound: when the paired channel is about to deliver, voice synthesizes a reply via `lib/voice/dashscope_tts.py` and the paired channel sends the audio plus text. Voice runs only when both sides agree (`channels.voice.enabled: true` AND `channels.<paired>.enabled: true`).
6. **`bin/jc-doctor` per-channel checks.**
   - discord: `DISCORD_BOT_TOKEN` set, `discord.py` importable.
   - jc-events: `state/events/` exists and writable.
   - cron: `heartbeat/tasks.yaml` parses; not double-running outside gateway.
   - voice: paired channel exists and is enabled; `DASHSCOPE_API_KEY` set; `lib/voice/*` reachable.
7. **`ops/gateway.yaml` schema additions.** Append:
   ```yaml
   channels:
     discord:
       enabled: false
       bot_token_env: DISCORD_BOT_TOKEN
     jc-events:
       enabled: true
       watch_dir: state/events
       poll_interval_seconds: 2
     cron:
       enabled: true
       tasks_file: heartbeat/tasks.yaml
     voice:
       enabled: false
       paired_with: telegram
       asr_provider: dashscope
       tts_provider: dashscope
   ```
   No `web:` entry. `config.py` rejects unknown channel keys with a clear error so old `web:` entries from any forked spec doc fail loudly rather than silently doing nothing.
8. **KB updates.** New files:
   - `docs/kb/subsystem/channel-discord.md`
   - `docs/kb/subsystem/channel-jc-events.md`
   - `docs/kb/subsystem/channel-cron.md`
   - `docs/kb/subsystem/channel-voice.md`
   - Update `docs/kb/subsystem/gateway-queue.md` "Open questions" — Discord no longer roadmap.

**Definition of done**

- Worker spawn → completion → single Telegram message synthesized by Rachel. No two-message UX.
- A scheduled heartbeat task hits the gateway queue, gets picked up, replied through Telegram. `jc heartbeat run --once` still works standalone.
- Discord + Telegram enabled simultaneously on a test instance; same Rachel replies on both.
- Voice round trip: Telegram voice memo → ASR → Rachel text reply → TTS → Telegram audio.
- `jc doctor` flags any channel misconfiguration without false positives.

**Out of scope:** Multi-brain, triage, brain overrides, web channel.

---

### Sprint 3 — Multi-brain

**Goal:** All four shipped brains usable as gateway brains, per-event brain selection (manual override or per-channel default), context loader for non-Claude brains, brain capability matrix.

**Deliverables**

1. **Per-brain Python wrappers** under `lib/gateway/brains/`:
   - `base.py` — `Brain` ABC with `async invoke(brain_config, context, prompt, session_id, instance_dir) -> BrainResponse`.
   - `claude.py` — wraps `lib/heartbeat/adapters/claude.sh`, captures `--resume <uuid>` session id from stderr.
   - `codex.py` — wraps `codex.sh`, resume via `codex exec resume <uuid>`, sandbox env from `brains.codex.sandbox`.
   - `gemini.py` — wraps `gemini.sh`, `GEMINI_YOLO` env from config.
   - `opencode.py` — wraps `opencode.sh`, resume via `--session`, prompt truncation already in shell adapter.
   - `aider.py` — new shell adapter `lib/heartbeat/adapters/aider.sh` plus Python wrapper. Conversation-history-file resume model: `(session_id == None) → start fresh, else load history file at <instance>/state/gateway/aider-sessions/<id>.json`.
   - `dispatch.py` — `invoke_brain(name, ...)` registry. Replaces direct call into `brain.py`. Old `brain.py` becomes a thin compat shim and is deleted at end of sprint once nothing imports it.
2. **`lib/gateway/context.py`.** Context loader.
   - For `brain == "claude"`: rely on `CLAUDE.md` auto-load; pass nothing extra.
   - For others: read `<instance>/memory/L1/*.md`, concatenate, prepend as `--system-prompt-file` (tools that support it) or stdin preamble (tools that do not). Cache the rendered preamble per-instance per-mtime in memory so we don't reread on every event.
3. **Brain override syntax.**
   - Inline prefix: `[opus] explain quantum tunneling` → `event.meta.brain_override = "claude:opus-4-7-1m"`. Stripped from `event.content` before brain invocation. Bracket pattern parsed in `lib/gateway/runtime.py` after queue claim, before router.
   - Slash command: `/brain claude:opus-4-7-1m` (or short form `/brain opus`) sets sticky brain for the conversation. Implemented as a system event handled by the runtime, not forwarded to a brain. Reply to the user is a one-liner ack.
   - Short-name resolution: `opus` → `claude:opus-4-7-1m`, `sonnet` → `claude:sonnet-4-6`, `haiku` → `claude:haiku-4-5`, `gpt5` → `codex:gpt-5`. Mapping lives in `lib/gateway/brains/aliases.py`.
4. **Per-channel + per-message brain config.**
   - `channels.<name>.brain` and `channels.<name>.model` already exist; verify they work end-to-end with non-Claude brains.
   - Per-message override beats sticky beats per-channel default beats `default_brain`.
5. **Brain capability matrix.** New contract doc `docs/kb/contract/brain-capabilities.md`:
   | Brain | Tools | Vision | File edits | Web | Resume |
   |-------|-------|--------|------------|-----|--------|
   | claude | yes | yes | yes | yes | `--resume <uuid>` |
   | codex | yes | partial | yes | no | `codex exec resume <uuid>` |
   | gemini | partial | yes | partial | yes | `--resume <uuid \| latest>` |
   | opencode | yes | no | yes | no | `--session <id>` |
   | aider | yes (git) | no | yes | no | history file |
   The triage layer (Sprint 4) consults this matrix to filter brain choices for image / multimodal events.
6. **`bin/jc-setup` prompt.** During guided instance setup, ask for default brain (claude/codex/gemini/opencode/aider). Default to claude. Write into `ops/gateway.yaml`.
7. **Tests.**
   - Unit: each brain wrapper called with a recorded subprocess fixture; assert the right argv and stdin. Resume id capture covered.
   - Integration: send a message with `[opus]` prefix; assert opus is invoked. Send a `/brain codex:gpt-5` slash; assert sticky updates and next message goes to codex.

**Definition of done**

- Codex / Gemini / OpenCode / Aider all work as the default brain end-to-end on a test instance.
- `[opus] ...` and `/brain ...` route correctly; sticky updates as expected.
- `docs/kb/contract/brain-capabilities.md` accurate and verified.
- `lib/gateway/brain.py` deleted (or empty shim warning on import).

**Out of scope:** Triage. Triage drives brain selection automatically using everything built here.

---

### Sprint 4 — Triage

**Goal:** All three triage backends shipped. Default brain selection automated. Sticky brain + confidence threshold + fallback in place.

**Deliverables**

1. **`lib/gateway/triage/`** module tree:
   - `base.py` — `TriageBackend` ABC: `async classify(event, instance_dir) -> TriageResult`.
   - `prompt.md` — shipped template (the one in `unified-gateway-0.3.0.md` §5.5 "Triage prompt", verbatim — do not paraphrase).
   - `result.py` — `TriageResult` dataclass: `class_`, `brain`, `confidence`, `reasoning`.
   - `ollama.py` — `httpx` POST to `${ollama_host}/api/generate`. Default model `phi3:mini`. Single-line JSON output parsed; malformed → confidence 0 → fallback.
   - `openrouter.py` — OpenAI-compatible client at OpenRouter base URL. Default model `meta-llama/llama-3.1-8b-instruct`. Reads `OPENROUTER_API_KEY` from `.env` via existing safe loader.
   - `claude_channel.py` — HTTP POST to local `jc-triage` plugin (see deliverable 2). Awaits response or falls back on timeout.
   - `cache.py` — short-lived in-memory LRU keyed by `hash(content)` so identical inbound messages within `triage_cache_ttl_seconds` (default 30) reuse the prior classification.
2. **`external_plugins/jc-triage/` plugin** (TypeScript, MCP server with `claude/channel` capability):
   - `server.ts` — opens HTTP listener on `${TRIAGE_PORT}` (default 9876). On gateway POST `/classify {message}`, plugin emits a channel notification to its host Claude session (Haiku). The host session replies via the plugin's `reply` tool; plugin captures and returns over HTTP.
   - `package.json`, `tsconfig.json`, install instructions in `external_plugins/jc-triage/README.md`.
   - Vendored install path: `templates/init-instance/.claude/plugins/jc-triage/`. New instances get the plugin scaffolded if `triage: claude-channel` is selected.
3. **Router wiring.** `lib/gateway/router.py` (created in Sprint 1) gains the full decision tree — order matters and is verified by unit test:
   ```
   1. event.meta.brain_override                  → use it, skip triage and sticky
   2. event.source == 'cron' and meta.brain      → use the task's pinned brain, skip triage
   3. sticky = sessions.get_active(channel, user)
      if sticky and now < sticky.sticky_until    → use sticky.brain, skip triage
   4. triage = backend.classify(event)
      if triage.confidence < threshold           → fallback brain
      else                                       → resolve_brain(triage.class_, triage.brain) per triage_routing map
   5. record sticky window: sessions.record_response(...)
   ```
4. **Sticky brain.** Stored in `sessions` table. Field `sticky_until = last_message_at + sticky_brain_idle_timeout_seconds` (default 600). Idle expiry forces re-triage on next message. `purge_idle()` housekeeping job in the runtime ticks every 5 minutes.
5. **`ops/gateway.yaml` triage block** (new):
   ```yaml
   triage: openrouter            # ollama | openrouter | claude-channel | always
   triage_confidence_threshold: 0.7
   default_fallback_brain: claude:sonnet-4-6
   triage_cache_ttl_seconds: 30
   sticky_brain_idle_timeout_seconds: 600
   triage_routing:
     smalltalk: claude:haiku-4-5
     quick:     claude:sonnet-4-6
     analysis:  claude:opus-4-7-1m
     code:      claude:sonnet-4-6
     image:     claude:sonnet-4-6
     voice:     claude:sonnet-4-6
     system:    claude:haiku-4-5
   ollama_model: phi3:mini
   ollama_host: http://localhost:11434
   ollama_timeout_seconds: 5
   openrouter_model: meta-llama/llama-3.1-8b-instruct
   openrouter_api_key_env: OPENROUTER_API_KEY
   openrouter_timeout_seconds: 5
   claude_triage_screen: jc-triage
   claude_triage_model: claude-haiku-4-5
   claude_triage_port: 9876
   ```
   `triage: always` skips triage entirely and routes everything to `default_fallback_brain` — useful for users who only want sticky brain semantics.
6. **`bin/jc-setup` triage prompt.** Ask: "Which triage backend? [openrouter/ollama/claude-channel/none]". On `ollama` selection, offer `curl https://ollama.ai/install.sh | sh` with explicit y/N consent, then `ollama pull phi3:mini`. On `claude-channel` selection, scaffold the `jc-triage` plugin. On `openrouter` selection, prompt for API key and write to `.env`.
7. **Metrics.** `state/gateway/triage-metrics.db` (SQLite). Counters per class, average confidence, override-after-triage rate (when `[brain]` prefix or `/brain` slash override fires after triage already chose). `jc gateway metrics` CLI prints last 24h.
8. **SIGHUP reload.** Switching `triage:` value in `ops/gateway.yaml` and sending SIGHUP swaps the backend without daemon restart. The runtime keeps an `Arc<TriageBackend>` (Python equivalent: `Lock`-guarded reference) that is replaced atomically.
9. **Tests.**
   - Unit: router.py decision tree (override → cron → sticky → triage → fallback). Each branch isolated with stubbed dependencies.
   - Unit: each triage backend's prompt parsing — fixture in / `TriageResult` out, including malformed JSON → fallback path.
   - Integration: deliberately ambiguous prompt forces fallback (confidence < 0.7); assert response comes from `default_fallback_brain`.
   - Integration: 5-message conversation; only message 1 hits triage; messages 2..5 reuse sticky brain.

**Definition of done**

- All three backends selectable via `triage:` and SIGHUP-swappable.
- Smalltalk → Haiku, analysis → Opus, code → Sonnet on the canonical examples in the prompt.
- Confidence-fallback path verified.
- Sticky brain prevents oscillation in a 5-message thread.
- `jc gateway metrics` reports realistic numbers after a day of use.

**Out of scope:** Memory updates from gateway brains (deferred to 0.3.1 per original spec §9.6). Cost-aware routing (deferred to 0.4.0).

---

### Sprint 5 — Migration, hardening, observability, docs, release

**Goal:** Safe migration path from 0.2.x. Production-quality reliability and observability. Released as `v0.3.0`.

**Deliverables**

1. **`bin/jc-migrate-to-0.3`.** Reads existing `ops/watchdog.conf` + `.env`. Writes `ops/gateway.yaml` with conservative defaults (telegram-only, single brain = current default, `triage: none` initially). Backs up `watchdog.conf` to `watchdog.conf.bak.<timestamp>`. Runs `jc doctor` before and after. Prints the next-step list (enable triage, add channels) to stdout. Idempotent: re-running on an already-migrated instance is a no-op.
2. **Reliability**
   - **Brain timeout enforcement.** Hard-kill brain subprocess after `brains.<brain>.timeout_seconds`. Mark event `failed` with `error="timeout"`. Retry honors `event_retry_backoff_seconds` array: `[10, 60, 300]`.
   - **Backpressure.** New config `max_queue_depth: 100`. When `SELECT count(*) FROM events WHERE status IN ('queued','running') >= max_queue_depth`, new inbound user events get a "system busy, retry shortly" reply via the originating channel; system events (`source` in `jc-events`, `cron`) are dropped with a log warning. Threshold checked at enqueue time.
   - **Log rotation.** `state/gateway/gateway.log` rotates at 50MB (size-based, 5 files retained). Use the standard `logging.handlers.RotatingFileHandler`.
   - **Idempotent retries.** Already covered by `(source, source_message_id)` unique index; verify retry path uses ON CONFLICT DO NOTHING semantics rather than failing.
3. **Observability**
   - Structured JSON gateway log. One JSON object per line. Required fields: `ts`, `level`, `event_id`, `source`, `user_id`, `class`, `brain`, `latency_ms`, `msg`.
   - `jc gateway logs --since 10m --class analysis --brain claude:opus-4-7-1m` filters the log. Plain-text `jc gateway tail` stays as-is (raw `tail -f`).
   - `jc gateway status` reports: pid, uptime, queue depth, last-N events with class + brain + latency.
4. **Tests**
   - Unit: `tests/gateway/test_router.py`, `test_sessions.py`, `test_queue.py`, `test_triage_*.py`, `test_brain_*.py`, `test_channel_*.py`. In-memory SQLite for queue + sessions tests.
   - Integration: end-to-end via `jc gateway enqueue` (no web channel) and a fake brain adapter shipped at `tests/gateway/fixtures/echo-brain.sh` that echoes its prompt. Asserts response arrives in queue with `status='done'` within 30s.
   - Smoke: `bin/test-gateway-smoke` script. Boots the gateway with the fake brain, enqueues a message, polls for `done`, exits non-zero on timeout. Wired into CI.
5. **Security audit (worker-driven)**
   - Spawn a Gemini worker on the new code with a bug-hunt prompt focused on channel input handling and queue state machine.
   - Spawn an Opus worker focused on injection paths: channel inputs reaching brain prompts, brain-prompt injection from inbound text, `.env` handling, plugin HTTP listener (jc-triage).
   - Both worker outputs land in `docs/kb/subsystem/security-audit-0.3.0.md` as a known-state snapshot (not a guarantee).
6. **Docs**
   - `docs/GATEWAY.md` — architecture, components, debugging, log filters, metrics.
   - `docs/MIGRATION-0.2-to-0.3.md` — step-by-step migration guide. Includes rollback (`RUNTIME_MODE=legacy-claude` in `ops/watchdog.conf`).
   - `docs/kb/decision/why-unified-gateway.md` — ADR. Cites the original 0.2.x pain points (single point of failure, worker→user friction, brain lock-in, no triage) and the architectural answers.
   - Update `QUICKSTART.md` so the default flow is gateway-first.
   - Update `ROADMAP.md`: tick 0.2.0 channel-plumbing item, add 0.3.0 line items.
   - Update `docs/kb/INDEX.md` to surface the new files.
7. **Watchdog**
   - When `RUNTIME_MODE=legacy-claude` is detected, log a deprecation warning each tick: "legacy-claude mode will be removed in 0.5.0; migrate via `jc migrate-to-0.3`".
8. **Release**
   - Tag `v0.3.0` after the spec author's instance has run for ≥7 days on the new gateway without intervention.
   - GitHub release notes generated from the sprint deliverable list.

**Definition of done**

- Production instance migrates with `jc migrate-to-0.3` and runs ≥7 days clean.
- Heartbeat tasks, voice, memory, worker spawn — no regression.
- Security audit captured in KB.
- `v0.3.0` tagged. Release notes posted.

---

## 4. Cross-cutting invariants

These hold across every sprint:

- **No `--channels` flag in adapter shell scripts under gateway mode.** Channel ownership belongs to the gateway runtime. The watchdog default args still include `--channels` for `legacy-claude` mode only.
- **No brain or network I/O inside a SQLite transaction.** Acquire data, commit, then call out.
- **Per-`(channel, user_id)` serialization at the queue worker.** Two messages from the same user wait their turn; cross-user is parallel.
- **State stays under `<instance>/state/`.** Templates ignore `state/`.
- **Adapter contract unchanged.** `lib/heartbeat/adapters/<tool>.sh`: `$1` = optional model, stdin = prompt, stdout = response, stderr = diagnostics. Resume env var = `JC_RESUME_SESSION` (with `WORKER_RESUME_SESSION` fallback).
- **Optional dependencies are gated imports.** `slack_bolt`, `discord.py`, `httpx`, `websocket-client`. Gateway boots without them; doctor reports the missing piece if the user enables a feature that needs it.
- **Web channel does not exist in 0.3.0.** Any reference to `web:` in config is a hard error from the YAML loader, with a hint pointing to `jc gateway enqueue` for local testing.

---

## 5. Open questions (carry-overs from original spec §9)

These remain open. Decide during the sprint that touches them.

1. **Voice as transparent transformer vs paired channel.** Sprint 2 ships paired. Revisit in 0.3.1 if usage shows the paired model is awkward.
2. **Per-user preferences override.** Defer to 0.3.x. Sticky + override syntax cover the immediate need.
3. **Common JC tool surface for non-Claude brains** (memory search, worker spawn from chat, etc.). 0.4.0+ — explicitly out of 0.3.0.
4. **Triage budget / rate limit.** Watch with the metrics from Sprint 4. If a misbehaving channel fires >1 classification/sec for the same user, add a per-user 30s triage cache (already covered by `triage_cache_ttl_seconds`).
5. **Memory updates from gateway brains.** Brain replies including `<memory_update slug="x">...</memory_update>` blocks parsed by the gateway. Spec the contract in 0.3.1.
6. **Concurrency model.** Per-`(channel, user)` serialization is the answer.

---

## 6. Decision Log

- 2026-04-25 — Spec authored to cover unshipped portions of original 0.3.0 design. Web channel dropped on user direction. Triage retained as the central deliverable. Sprint count kept at five for rough parity with the original.
