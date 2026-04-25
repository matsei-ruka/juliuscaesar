# Spec: Unified Gateway (JC 0.3.0)

**Status:** Draft — pending Luca review
**Author:** Rachel
**Date:** 2026-04-25
**Branch:** `0.3.0-unified-gateway`

---

## 1. Problem

JuliusCaesar 0.2.x is architecturally coupled to Claude Code via the Telegram MCP plugin. Three pain points have surfaced repeatedly:

1. **Single point of failure.** When the Telegram plugin dies (which happens under load), inbound and outbound traffic both go dark. The 0.2.x watchdog now restarts cleanly (commit `4861d57`), but the underlying coupling remains: one plugin = one channel = one liveness path.

2. **Worker → Rachel is hand-driven.** When a `jc workers` job finishes, the runner sends a raw "worker #N done" Telegram message to the user, who must then ask Rachel for a synthesis. Two messages to learn one fact. The worker can't push directly into Rachel's session because there's no event channel — only the user-facing Telegram channel.

3. **Brain lock-in.** Claude Code is the only supported live brain. Heartbeat is brain-agnostic for scheduled tasks (it shells out to adapter scripts), but the interactive layer (Telegram → live Rachel) is Claude-specific. External users without a Claude subscription have no path to a working setup.

Layered on top: every message — whether trivial small talk or a deep analysis request — pays full Sonnet/Opus pricing. There's no triage.

## 2. Goal

Build a **unified gateway** that:

- Accepts inputs from any number of channels (Telegram, Slack, Discord, web UI, worker events, watchdog alerts, voice, cron).
- Triages each input via a configurable lightweight LLM that picks the right brain and brain-config for the task.
- Invokes the selected brain non-interactively per event (`claude -p --resume`, `codex exec resume`, `gemini -p`, `opencode run --session`).
- Routes the response back through the appropriate outbound channel(s).
- Persists per-brain session state so resume-style continuity works regardless of brain choice.

The gateway is the natural evolution of the heartbeat runner: heartbeat already invokes brains non-interactively in response to cron events. The gateway extends the same pattern to push events from channels.

## 3. Non-goals

- **Replacing Claude Code as the default brain.** It remains the recommended default. The gateway makes alternatives possible, not mandatory.
- **Building a polished web UI in 0.3.0.** The web channel ships as a minimal Bun-served localhost UI (modeled on the existing `fakechat` plugin). Polished web UX is 0.3.1+.
- **Replacing memory, voice, or heartbeat-cron.** Those subsystems stay; the gateway integrates with them.
- **Dropping the live interactive Claude session entirely in 0.2.x.** The gateway is opt-in via `BRAIN_RUNTIME=gateway` in `ops/gateway.conf`. 0.2.x users keep working until they choose to migrate.
- **Multi-tenant SaaS.** That's a downstream payoff, not a 0.3.0 deliverable.

## 4. Architecture

### 4.1 Pipeline

```
INBOUND CHANNELS                   GATEWAY                          BRAIN ADAPTERS
- telegram          ┌─────────►   ┌──────────────────┐    ┌────►   - claude
- slack             │             │  event queue     │    │        - codex
- discord           │             │  triage          │    │        - gemini
- web (fakechat)   ─┤             │  router          ├────┤        - opencode
- jc-events         │             │  session manager │    │        - aider
- voice             │             │  context loader  │    │
- cron (heartbeat) ─┘             └──────────────────┘    │
                                                          │
OUTBOUND CHANNELS  ◄──────────────────────────────────────┘
- telegram
- slack
- discord
- web
- voice (TTS)
- file (worker result writeback)
```

### 4.2 Component overview

| Component | Lives in | Process model |
|-----------|----------|---------------|
| Gateway daemon | `bin/jc-gateway`, `lib/gateway/*.py` | Long-running Python daemon, one per instance, supervised by watchdog |
| Event queue | `state/gateway/queue.db` (SQLite) | Persistent FIFO with retry + dedup |
| Channel adapters | `lib/gateway/channels/*.py` | In-process plugins loaded by the gateway |
| Brain adapters | `lib/gateway/brains/*.py` (with shell helpers `lib/heartbeat/adapters/*.sh` reused) | Subprocess invocation per event |
| Triage | `lib/gateway/triage/*.py` | Configurable: ollama / openrouter / claude-haiku |
| Session manager | `lib/gateway/sessions.py`, persists to `state/gateway/sessions.db` | In-process, with disk persistence |
| Router | `lib/gateway/router.py` | In-process, deterministic given triage output |

### 4.3 Why one daemon, not many

A single daemon was considered against alternatives (per-channel processes, MCP plugins for everything). Single daemon wins because:

- **Shared state.** Sessions, sticky-brain, queue, and dedup all need coordinated access. Cross-process IPC for these is expensive.
- **Failure surface.** One daemon to supervise vs. N. Watchdog already handles this pattern.
- **Adoption path.** Heartbeat already runs as a single daemon (cron + lib/heartbeat/runner.py). The gateway extends it; users don't get a new mental model.

The cost is a fatter daemon. Mitigated by keeping channel and brain adapters as cleanly separable modules. If 0.4.0 needs to split, the boundaries are already there.

---

## 5. Components

### 5.1 Gateway daemon

**Lifecycle**

```
jc-gateway start    → fork + setsid, write pid to state/gateway/jc-gateway.pid
jc-gateway stop     → SIGTERM the pid, wait up to 10s, SIGKILL
jc-gateway restart  → stop + start
jc-gateway status   → read pid, check liveness, print queue depth + recent activity
jc-gateway tail     → tail -f state/gateway/gateway.log
```

**Watchdog integration**

`lib/watchdog/watchdog.sh` learns to supervise the gateway:

```bash
gateway_alive() {
    [[ -f "$INSTANCE_DIR/state/gateway/jc-gateway.pid" ]] || return 1
    local pid
    pid=$(cat "$INSTANCE_DIR/state/gateway/jc-gateway.pid")
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}
```

Watchdog tick adds: `gateway_alive` check; if dead and `BRAIN_RUNTIME=gateway`, restart it.

**Configuration source of truth**

`ops/gateway.conf` is the single config file. Format below in §6. Reloaded on SIGHUP.

### 5.2 Event queue

Backed by SQLite at `state/gateway/queue.db`:

```sql
CREATE TABLE events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,            -- channel name, e.g. 'telegram', 'jc-events'
    source_message_id TEXT,                   -- channel-native id, used for dedup
    user_id         TEXT,                     -- channel-native user identifier (e.g. telegram chat_id)
    content         TEXT NOT NULL,            -- raw inbound text
    meta            TEXT,                     -- JSON blob (file paths, reply_to, etc.)
    received_at     TEXT NOT NULL,            -- ISO8601 UTC
    triaged_at      TEXT,
    triage_class    TEXT,                     -- smalltalk | quick | analysis | code | image | voice | system | unsafe
    triage_brain    TEXT,                     -- selected brain, e.g. 'claude:haiku-4-5'
    triage_confidence REAL,
    started_at      TEXT,                     -- when brain invocation began
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'queued',  -- queued | triaging | running | done | failed | rejected
    response        TEXT,                     -- final brain response
    error           TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX idx_events_dedup ON events(source, source_message_id) WHERE source_message_id IS NOT NULL;
CREATE INDEX idx_events_status ON events(status);
CREATE INDEX idx_events_received ON events(received_at DESC);
```

**Invariants**

- Inbound channels enqueue events with `(source, source_message_id)` populated when available — the unique index gives free at-most-once semantics for replays.
- The queue worker advances events through states; it never deletes (history is the audit trail).
- Retry: failed events with `retry_count < MAX_RETRIES` get re-queued with exponential backoff. Default `MAX_RETRIES=3`.

### 5.3 Channel abstraction

A channel is a Python module exposing two coroutines:

```python
async def inbound(emit: Callable[[Event], Awaitable[None]]) -> None:
    """Long-running task. Reads the channel and calls `emit(event)` for each message."""

async def outbound(event: Event, response: Response) -> None:
    """Send `response` back to the originating user via this channel."""
```

Plus channel metadata:

```python
NAME: str = "telegram"
DIRECTION: Literal["in", "out", "both"] = "both"
CONFIG_KEYS: list[str] = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
```

Built-in channels for 0.3.0:

| Channel | In | Out | Notes |
|---------|----|----|-------|
| telegram | ✓ | ✓ | Replaces the MCP plugin path (long-poll Bot API directly from Python) |
| slack | ✓ | ✓ | Socket Mode via `slack_bolt`; SLACK_APP_TOKEN + SLACK_BOT_TOKEN |
| discord | ✓ | ✓ | `discord.py` gateway intent; DISCORD_BOT_TOKEN |
| web | ✓ | ✓ | Bun-served localhost UI (fakechat-style); WEB_PORT |
| jc-events | ✓ | ✗ | Worker / system events; watches `state/events/` via inotify |
| voice | ✓ | ✓ | DashScope ASR/TTS hooks (uses `lib/voice/*`); only paired with another channel for I/O |
| cron | ✓ | ✗ | Re-uses `lib/heartbeat/runner.py` to push scheduled events into the queue |

Channels are loaded at startup based on which `<CHANNEL>_ENABLED=1` flags are set in `ops/gateway.conf`.

### 5.4 Brain adapter contract

Each brain is a Python module with one coroutine:

```python
async def invoke(
    brain_config: BrainConfig,   # selected model, params
    context: list[Message],      # rendered history + context
    prompt: str,                 # the user's current message or system prompt
    session_id: Optional[str],   # for --resume
    instance_dir: Path,
) -> BrainResponse:
    """Run the brain non-interactively and return its response + new session_id."""
```

The Python module is a thin wrapper around the existing shell adapters in `lib/heartbeat/adapters/*.sh`. The shell scripts already accept stdin prompts and output stdout responses; the Python wrapper handles `--resume` flag injection, session ID capture, timeout, and error mapping.

Built-in brain adapters:

| Brain | Models supported | Resume mechanism |
|-------|------------------|------------------|
| claude | haiku-4-5, sonnet-4-6, opus-4-7, opus-4-7-1m | `--resume <uuid>` |
| codex | gpt-5, gpt-4o, gpt-4o-mini | `codex exec resume <uuid>` |
| gemini | gemini-2.0-flash, gemini-2.5-pro | `--resume <uuid \| latest>` |
| opencode | configurable | `opencode run --session <id>` |
| aider | configurable | conversation history file |

### 5.5 Triage layer

Triage is a stage that runs **before** brain invocation, on every inbound event. It returns:

```python
@dataclass
class TriageResult:
    class_: Literal["smalltalk","quick","analysis","code","image","voice","system","unsafe"]
    brain: str                  # e.g. "claude:haiku-4-5", "codex:gpt-5"
    confidence: float           # 0.0–1.0
    reasoning: Optional[str]    # for debug logs
```

**Three triage backends, configurable per instance:**

#### 5.5.1 `triage=ollama`

Local model via [ollama](https://ollama.ai/). Default model: `phi3:mini` (3.8B params, ~2.3GB on disk, ~150ms per classification on CPU).

```yaml
triage: ollama
ollama_model: phi3:mini
ollama_host: http://localhost:11434
```

**Pros:** zero marginal cost, no API keys, fully local.
**Cons:** requires ollama installed + running; slower than cloud options on slow hardware; first-token latency includes model load if unloaded.

#### 5.5.2 `triage=openrouter`

Cloud router with model choice. Default: `meta-llama/llama-3.1-8b-instruct` via OpenRouter (~50ms, ~$0.05/M tokens).

```yaml
triage: openrouter
openrouter_model: meta-llama/llama-3.1-8b-instruct
openrouter_api_key_env: OPENROUTER_API_KEY    # read from .env
```

**Pros:** fast, cheap, no local resources, simple BYOK.
**Cons:** requires API key; cloud dependency; rate limits per tier.

This is the default for users who don't want to run local models. Luca's choice for Rachel.

#### 5.5.3 `triage=claude-channel`

Long-running Claude session with Haiku, attached via a dedicated MCP triage channel. Reuses Claude infrastructure for users who already pay for Claude.

```yaml
triage: claude-channel
claude_triage_screen: rachel-triage     # dedicated screen session name
claude_triage_model: claude-haiku-4-5
```

**How it works:**

1. At gateway start, spawn a second Claude process: `claude --model claude-haiku-4-5 --channels plugin:jc-triage@<marketplace> --dangerously-skip-permissions` in a screen session.
2. The `jc-triage` plugin (new, ships in 0.3.0) accepts gateway HTTP POSTs at `localhost:<TRIAGE_PORT>/classify`, emits a channel notification to the Claude-haiku session, captures the reply tool's output, returns it to the gateway.
3. Gateway polls / awaits the response.

**Pros:** uses existing Claude subscription; consistent with the rest of JC; benefits from Claude's classification accuracy.
**Cons:** two Claude processes running on the host (memory + supervision overhead); extra MCP plugin to ship and audit; not free for non-Claude users.

#### Triage prompt

All three backends use the same prompt template. Stored in `lib/gateway/triage/prompt.md`:

```
You are a triage classifier. You output exactly one JSON object on a single line.

Schema: {"class":"<class>","brain":"<brain>","confidence":<0..1>}

Classes and their default brains:
- smalltalk     → claude:haiku-4-5     (greetings, banter, quick chitchat)
- quick         → claude:sonnet-4-6    (single-step questions, < 1 min work)
- analysis      → claude:opus-4-7-1m   (research, comparison, multi-step reasoning)
- code          → claude:sonnet-4-6    (build, edit, refactor, debug)
- image         → claude:sonnet-4-6    (multimodal, image gen, image read)
- voice         → claude:sonnet-4-6    (transcribed voice; re-triage on text)
- system        → claude:haiku-4-5     (worker events, watchdog alerts, scheduled tasks)
- unsafe        → reject               (out-of-policy; do not invoke a brain)

Pick the class. Then pick the brain — usually the default for that class, but you may override if the message clearly demands more or less power.

Examples:
- "what time is it?" → {"class":"smalltalk","brain":"claude:haiku-4-5","confidence":0.95}
- "compare three esim providers and recommend one for Dubai" → {"class":"analysis","brain":"claude:opus-4-7-1m","confidence":0.9}
- "fix the off-by-one in the auth handler" → {"class":"code","brain":"claude:sonnet-4-6","confidence":0.95}
- "worker #18 done" → {"class":"system","brain":"claude:haiku-4-5","confidence":1.0}

Now classify this message:
{message}
```

The default brains are configurable per-instance via `ops/gateway.conf` `triage_routing:` block (§6). The above is the shipped default.

#### Confidence threshold and fallback

If `confidence < 0.7`, the router falls back to `default_fallback_brain` (configurable; default `claude:sonnet-4-6`). This avoids miscategorizing edge cases into the wrong tier.

#### Sticky brain + idle timeout

Once a brain is selected for a conversation, subsequent messages stick to that brain until the conversation goes idle (`STICKY_BRAIN_IDLE_TIMEOUT_SECONDS`, default 600 = 10 min). After idle, the next message is re-triaged.

The session manager (§5.6) tracks `last_brain` per `(channel, user_id)` pair. The router consults it before invoking triage.

### 5.6 Session manager

State stored at `state/gateway/sessions.db`:

```sql
CREATE TABLE sessions (
    channel         TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    brain           TEXT NOT NULL,            -- e.g. 'claude:sonnet-4-6'
    session_id      TEXT,                     -- brain-native session id for resume
    last_message_at TEXT NOT NULL,
    sticky_until    TEXT,                     -- after this, next message re-triages
    PRIMARY KEY (channel, user_id, brain)
);
```

**Operations**

- `get_active(channel, user_id) -> Optional[BrainSelection]` — returns the sticky brain if `now < sticky_until`, else None (forces re-triage).
- `record_response(channel, user_id, brain, session_id)` — updates `last_message_at`, extends `sticky_until = now + STICKY_BRAIN_IDLE_TIMEOUT_SECONDS`, captures the post-invocation brain-native session id for next resume.
- `purge_idle()` — periodic task; deletes rows whose `sticky_until < now - 7 days`.

### 5.7 Router

Pure logic given (event, triage_result, sticky_state):

```
if event.source == 'cron':
    return cron_brain_from_task_config(event)

sticky = sessions.get_active(event.source, event.user_id)
if sticky is not None:
    return sticky.brain  # skip triage entirely

triage = triage_backend.classify(event)
if triage.confidence < CONFIDENCE_THRESHOLD:
    return DEFAULT_FALLBACK_BRAIN
return resolve_brain(triage.class, triage.brain)  # consult triage_routing override map
```

The router has no I/O; it's testable in isolation.

---

## 6. Configuration

### 6.1 `ops/gateway.conf`

Full reference (YAML; all keys have defaults):

```yaml
# JuliusCaesar Gateway configuration. This file is read at gateway start
# and on SIGHUP. Comments allowed. All keys optional; defaults shown.

# --- Runtime mode ---
brain_runtime: gateway              # gateway | mcp-plugin (legacy 0.2.x)

# --- Triage ---
triage: openrouter                  # ollama | openrouter | claude-channel | always
                                    # 'always' = no triage, route everything to default_brain

# Confidence threshold for accepting triage output
triage_confidence_threshold: 0.7
default_fallback_brain: claude:sonnet-4-6

# Per-class default brain (overrides shipped defaults)
triage_routing:
  smalltalk:  claude:haiku-4-5
  quick:      claude:sonnet-4-6
  analysis:   claude:opus-4-7-1m
  code:       claude:sonnet-4-6
  image:      claude:sonnet-4-6
  voice:      claude:sonnet-4-6
  system:     claude:haiku-4-5

# Sticky brain
sticky_brain_idle_timeout_seconds: 600

# --- Triage backend: ollama ---
ollama_model: phi3:mini
ollama_host: http://localhost:11434
ollama_timeout_seconds: 5

# --- Triage backend: openrouter ---
openrouter_model: meta-llama/llama-3.1-8b-instruct
openrouter_api_key_env: OPENROUTER_API_KEY
openrouter_timeout_seconds: 5

# --- Triage backend: claude-channel ---
claude_triage_screen: jc-triage
claude_triage_model: claude-haiku-4-5
claude_triage_port: 9876

# --- Channels (set _enabled: true to activate) ---
channels:
  telegram:
    enabled: true
    bot_token_env: TELEGRAM_BOT_TOKEN
    chat_id_env: TELEGRAM_CHAT_ID
  slack:
    enabled: false
    app_token_env: SLACK_APP_TOKEN
    bot_token_env: SLACK_BOT_TOKEN
    workspace: my-team
  discord:
    enabled: false
    bot_token_env: DISCORD_BOT_TOKEN
  web:
    enabled: false
    port: 8787
    bind: 127.0.0.1
  jc-events:
    enabled: true
    watch_dir: state/events
  voice:
    enabled: false
    paired_with: telegram           # which I/O channel relays the voice
    asr_provider: dashscope
    tts_provider: dashscope
  cron:
    enabled: true
    tasks_file: heartbeat/tasks.yaml

# --- Brain adapter overrides ---
brains:
  claude:
    bin: claude
    extra_args:
      - --dangerously-skip-permissions
      - --chrome
    timeout_seconds: 600
  codex:
    bin: codex
    sandbox: workspace-write
    timeout_seconds: 900
  gemini:
    bin: gemini
    yolo: true
    timeout_seconds: 600
  opencode:
    bin: opencode
    timeout_seconds: 600

# --- Reliability ---
event_max_retries: 3
event_retry_backoff_seconds: [10, 60, 300]
queue_purge_after_days: 30

# --- Logging ---
log_level: info                     # debug | info | warn | error
log_path: state/gateway/gateway.log
```

### 6.2 Secrets in `.env`

The gateway uses the same `.env` pattern hardened in commit `6912560`. Loaded by the `load_env_file` parser (no shell sourcing), only known keys are exported.

```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN=xoxb-...
DISCORD_BOT_TOKEN=...
OPENROUTER_API_KEY=sk-or-...
DASHSCOPE_API_KEY=sk-...
```

---

## 7. Sprint Plan

Five sprints, ~1 week each. Strict order: each sprint depends on the previous.

### Sprint 1 — Gateway Foundation

**Goal:** A gateway daemon that accepts events from one channel (telegram), invokes one brain (claude), and replies. No triage, no multi-brain, no other channels. Pure plumbing.

**Deliverables:**

1. `lib/gateway/__init__.py`, `gateway.py` (main loop)
2. `lib/gateway/queue.py` — SQLite event queue (schema in §5.2)
3. `lib/gateway/sessions.py` — session manager (schema in §5.6)
4. `lib/gateway/router.py` — pure routing logic (initially: always pick `claude:sonnet-4-6`)
5. `lib/gateway/channels/telegram.py` — long-poll Bot API → enqueue inbound; implement `outbound()` via Bot API send
6. `lib/gateway/brains/__init__.py`, `claude.py` — non-interactive `claude -p --resume` wrapper, captures session id post-call
7. `bin/jc-gateway` — start/stop/restart/status/tail subcommands
8. `lib/watchdog/watchdog.sh` — add `gateway_alive()` check + restart on `BRAIN_RUNTIME=gateway`
9. `bin/jc-doctor` — gateway section: process alive, queue file present, channels module loadable, brain bin on path
10. Migration shim: when `BRAIN_RUNTIME=mcp-plugin`, gateway daemon is not started; legacy 0.2.x path runs untouched

**Definition of done:**

- New instance with `BRAIN_RUNTIME=gateway` starts via `jc setup --start`.
- Sending a Telegram message reaches Rachel via the gateway, gets a Sonnet reply, returns to Telegram.
- Killing the gateway daemon → watchdog restarts within 2 minutes.
- Toggling `BRAIN_RUNTIME=mcp-plugin` reverts to 0.2.x behavior — no regression.
- `jc doctor` reports gateway health.

**Files touched (rough):**

- New: 9 files under `lib/gateway/` (~1500 LOC Python total)
- New: `bin/jc-gateway` (~250 LOC bash dispatcher)
- Modified: `lib/watchdog/watchdog.sh` (+30 LOC), `bin/jc-doctor` (+40 LOC), `bin/jc-setup` (+30 LOC for `BRAIN_RUNTIME` prompt)

**Out of scope:** triage, additional channels, additional brains, voice, slack, discord, web. Those land in later sprints.

**Risk register:**

- Long-poll Telegram from Python (no MCP plugin): need to handle 409 conflicts if the legacy plugin is still polling the same bot. Mitigation: doctor check refuses to start gateway if telegram MCP plugin pid file exists.
- Session id capture race: two Telegram messages in rapid succession before the first `claude -p` finishes capturing its session id. Mitigation: serialize per-`(channel, user)` in the queue worker.

### Sprint 2 — Multi-Channel

**Goal:** All planned input/output channels working. No triage yet.

**Deliverables:**

1. `lib/gateway/channels/jc-events.py` — inotify watcher on `state/events/`. Worker / system event JSON schema:
   ```json
   {
     "event_type": "worker.completed",
     "worker_id": 18,
     "topic": "fix bugs",
     "status": "done",
     "duration_seconds": 145
   }
   ```
   Emits as system-class events with content `"worker #18 done — please synthesize for the user"`.
2. `bin/jc-workers` — replace the `_format_notification` Telegram-direct call with: write event JSON to `state/events/<id>-completed.json`. Backwards-compat flag `WORKERS_NOTIFY_MODE=telegram-direct` preserves 0.2.x behavior.
3. `lib/gateway/channels/slack.py` — Socket Mode via `slack_bolt`. Inbound: reactions, mentions, DMs. Outbound: `mrkdwn` formatted reply to thread.
4. `lib/gateway/channels/discord.py` — gateway intent via `discord.py`. Inbound: DMs + mentions. Outbound: thread reply.
5. `lib/gateway/channels/web.py` — Bun-served localhost UI (port 8787 by default). Reuse `external_plugins/fakechat/server.ts` HTML, but the Bun process is now spawned and supervised by the gateway.
6. `lib/gateway/channels/voice.py` — pairs with another I/O channel; transcribes via DashScope; pushes a text event into the queue with `meta.original_voice_path`.
7. `lib/gateway/channels/cron.py` — adapter over `lib/heartbeat/runner.py`. Existing scheduled tasks now emit events into the gateway queue instead of invoking adapters directly. Backwards-compat: `lib/heartbeat/runner.py` keeps its current entry point so `jc heartbeat run` still works standalone.
8. `bin/jc-doctor` — per-channel checks (token present, plugin reachable, port free).
9. Per-channel docs in `docs/kb/subsystem/channel-*.md`.

**Definition of done:**

- Five channels enabled simultaneously on a test instance, all delivering messages to Rachel and receiving replies.
- A `jc workers spawn` completion fires through `jc-events`, Rachel auto-synthesizes, sends one Telegram message. Two-message UX gone.
- A scheduled heartbeat task delivers via the gateway, indistinguishable from before.
- Disabling a channel via `enabled: false` doesn't break the others.

**Files touched:**

- 7 new channel modules (~2500 LOC total)
- `bin/jc-workers` modified (~50 LOC delta)
- `bin/jc-doctor` extended (+100 LOC)
- New docs: 5 files in `docs/kb/subsystem/`

**Risk register:**

- Discord and Slack require live OAuth setup — instances can't fully test without real workspaces. Mitigation: integration tests gated on env vars; CI runs the in-memory web channel only.
- Voice transcription latency adds ~2s. Acceptable; document in voice channel docs.

### Sprint 3 — Multi-Brain

**Goal:** All planned brain adapters working as gateway brains. Per-event brain choice via config. Still no triage.

**Deliverables:**

1. `lib/gateway/brains/codex.py` — wraps `lib/heartbeat/adapters/codex.sh`. Resume via `codex exec resume <uuid>`. Sandbox env from `brains.codex.sandbox`.
2. `lib/gateway/brains/gemini.py` — wraps `gemini.sh`. YOLO env from config.
3. `lib/gateway/brains/opencode.py` — wraps `opencode.sh`. Resume via `--session`.
4. `lib/gateway/brains/aider.py` — new adapter; conversation-history-file resume model.
5. `lib/gateway/context.py` — context loader. For Claude: relies on CLAUDE.md auto-load. For others: reads L1 markdown files, concatenates, prepends as `--system-prompt-file`.
6. `lib/gateway/sessions.py` — extend session capture per brain (reuse `_capture_session_id` patterns from `bin/jc-workers` after Sprint 1's named-worker bug fix).
7. `bin/jc-setup` — prompt for default brain during instance setup.
8. Brain capability matrix in `docs/kb/contract/brain-capabilities.md`: which brains support tools, vision, file edits, web, voice. Used by triage to pick valid brains.

**Definition of done:**

- Single instance can be configured to use Codex (or any other brain) as the default. End-to-end Telegram → Codex → reply works.
- A user-explicit override via slash command `/brain opus` (or per-message prefix `[opus]`) routes that one message to a different brain.
- Resume works across invocations for each brain individually.

**Files touched:**

- 4 new brain adapter modules (~1200 LOC Python total)
- `lib/gateway/context.py` (~400 LOC)
- `bin/jc-setup` (+50 LOC)
- New doc: `docs/kb/contract/brain-capabilities.md`

**Risk register:**

- Aider session-resume model differs significantly. Mitigation: aider adapter accepts `(session_id == None) → start fresh, else load from history file path`. Document the limitation.
- OpenCode session listing returns plain text in some versions (per Sprint 1 fix to gemini parsing). Apply same robustness here.

### Sprint 4 — Triage

**Goal:** All three triage backends shipped. Default brain selection automated.

**Deliverables:**

1. `lib/gateway/triage/__init__.py`, `base.py` — abstract base class `TriageBackend`.
2. `lib/gateway/triage/ollama.py` — `httpx` POST to `${ollama_host}/api/generate` with the triage prompt; parses single-line JSON.
3. `lib/gateway/triage/openrouter.py` — same prompt, OpenRouter `/chat/completions` API. Uses `openai`-compatible client with custom base URL.
4. `lib/gateway/triage/claude_channel.py` — HTTP POST to the local `jc-triage` plugin (see below); awaits classification.
5. **New plugin:** `external_plugins/jc-triage/` (in the user's `~/.claude/plugins/marketplaces/...` or vendored in this repo as `templates/init-instance/.claude/plugins/`).
   - `server.ts`: stdio MCP server with `claude/channel` capability.
   - On start, also opens an HTTP listener on `${TRIAGE_PORT}`.
   - When gateway POSTs `/classify` with `{message}`, plugin emits a channel notification to its host Claude session.
   - The host Claude session (running with Haiku) responds via the plugin's `reply` tool, which the plugin captures and returns over HTTP.
6. `lib/gateway/triage/prompt.md` — shipped prompt (in §5.5).
7. `lib/gateway/router.py` — wire triage into the routing decision; honor sticky-brain.
8. `bin/jc-setup` — prompt for triage backend during setup; install ollama model if chosen.
9. Metrics: per-class counts, average confidence, miscategorization rate (when user explicitly overrides brain after triage chose otherwise — log for offline analysis).

**Definition of done:**

- Three triage modes selectable via `triage:` config key. Switching takes effect on SIGHUP without daemon restart.
- Smalltalk reliably routes to Haiku; analysis reliably to Opus.
- Confidence-threshold fallback verified with a deliberately-ambiguous test message.
- Sticky brain prevents oscillation: a 5-message conversation stays on the brain selected for message 1.

**Files touched:**

- 4 new modules in `lib/gateway/triage/` (~700 LOC Python total)
- New plugin `jc-triage` (~250 LOC TypeScript)
- `lib/gateway/router.py` extended (+150 LOC)
- `bin/jc-setup` (+40 LOC)

**Risk register:**

- Triage misclassification of user intent. Mitigation: confidence threshold, sticky brain, user override commands (`/brain X`). Long-term mitigation: log mismatches and periodically retrain the prompt.
- ollama not installed locally. Mitigation: setup wizard offers to `curl https://ollama.ai/install.sh | sh` with explicit user consent; otherwise instructs.
- claude-channel triage adds 1.5–3s latency per message (extra Claude cold-start within the channel). Mitigation: keep the triage Claude session warm by pinging it every 60s; document the latency cost.

### Sprint 5 — Migration & Hardening

**Goal:** Safe migration path from 0.2.x. Production-quality reliability and observability.

**Deliverables:**

1. `bin/jc-migrate-to-0.3` — automated migration tool. Reads existing `ops/watchdog.conf` + `.env`, writes `ops/gateway.conf` with conservative defaults (telegram-only, single-brain, no triage). Doctors before and after.
2. **Reliability:**
   - Queue worker idempotency: events with the same `(source, source_message_id)` are deduplicated on retry.
   - Brain timeout enforcement: hard-kill the subprocess if it exceeds `brains.<brain>.timeout_seconds`.
   - OOM-safe log handling: gateway log rotates at 50MB.
   - Backpressure: if queue depth exceeds `MAX_QUEUE_DEPTH=100`, new inbound events get a "system busy, retry shortly" reply (or are dropped silently for system events).
3. **Observability:**
   - `jc gateway status` shows: pid, uptime, queue depth, last-N events with class+brain+latency.
   - `jc gateway tail` streams the gateway log.
   - `state/gateway/gateway.log` uses structured JSON; `jc gateway logs --since 10m --class analysis` filters.
4. **Tests:**
   - Unit: router.py, sessions.py, queue.py (with in-memory SQLite).
   - Integration: end-to-end via `web` channel + a fake brain adapter that echoes the prompt. Runs in CI.
   - Smoke: `bin/test-gateway-smoke` script that posts a message via the web channel, asserts the response arrives within 30s.
5. **Docs:**
   - Update `QUICKSTART.md` for 0.3.0 default mode.
   - New: `docs/GATEWAY.md` — architecture, components, debugging.
   - New: `docs/kb/decision/why-unified-gateway.md` — ADR.
   - Migration guide in `docs/MIGRATION-0.2-to-0.3.md`.
6. **Watchdog improvements:**
   - When MCP-plugin path is deprecated, warn user that 0.4.0 will remove it.
7. **Security audit:**
   - Spawn a Gemini bug-hunt worker on the new code.
   - Spawn an Opus security-audit worker focused on injection paths (channel inputs, brain prompt injection, .env handling).
8. **Versioning:**
   - Tag `v0.3.0` after Sprint 5 completes and Rachel-instance-on-this-spec has run for ≥7 days without intervention.

**Definition of done:**

- Rachel migrates from 0.2.x to 0.3.0 with `bin/jc-migrate-to-0.3` and runs cleanly for a week.
- Sergio (or any external user) migrates with the same tool.
- No regression on heartbeat scheduled tasks, voice, memory, or worker spawn.
- 0.3.0 tagged and released.

**Files touched:**

- New: `bin/jc-migrate-to-0.3`, `bin/test-gateway-smoke` (~400 LOC bash combined)
- Tests: `tests/gateway/*.py` (~1500 LOC)
- Docs: 4 new markdown files
- Watchdog + doctor minor edits

---

## 8. Migration Plan

### 8.1 Migration triggers

A user migrates when they want any of:

- Multiple channels (Telegram + Slack, etc.)
- A non-Claude brain
- Triage / cost optimization
- Worker → auto-synthesis UX

A user **does not need** to migrate if 0.2.x meets their needs. The gateway is opt-in.

### 8.2 Migration steps

1. `git pull` JC framework to 0.3.0.
2. `cd <instance> && jc migrate-to-0.3`. The tool:
   - Reads `ops/watchdog.conf` and `.env`.
   - Generates `ops/gateway.conf` (`brain_runtime: gateway`, telegram-only, single Sonnet brain, no triage).
   - Backs up the old `watchdog.conf` to `watchdog.conf.bak`.
3. `jc doctor` to verify.
4. `jc gateway start` (or watchdog restart).
5. Test send a Telegram message; verify reply.
6. Once stable, enable triage: edit `ops/gateway.conf`, set `triage: openrouter`, add `OPENROUTER_API_KEY` to `.env`, `kill -HUP $(cat state/gateway/jc-gateway.pid)`.
7. Add additional channels incrementally.

### 8.3 Rollback

`brain_runtime: mcp-plugin` in `ops/gateway.conf` reverts to 0.2.x behavior. Watchdog stops the gateway daemon and resumes the legacy live-Claude path. No data loss; queue events from 0.3.0 mode persist in `state/gateway/queue.db` and re-process on next migration.

### 8.4 Deprecation timeline

- 0.3.0: gateway opt-in, MCP-plugin path supported.
- 0.3.x patches: bug fixes for both paths.
- 0.4.0 (estimated 2026-Q3): MCP-plugin path deprecated with warnings.
- 0.5.0 (estimated 2026-Q4): MCP-plugin path removed; gateway is the only path.

---

## 9. Open Questions

1. **Voice channel architecture.** Does voice live as its own channel that pairs with another (current proposal), or as a transparent transformer applied to any channel? The current proposal is simpler. The transformer model would let voice work over Slack and Discord with no extra config. Defer to Sprint 2 implementation experience.

2. **Web channel UX.** The Bun-served fakechat-style UI is minimal. Do we ship a richer UI in 0.3.0 or defer? Current proposal: defer — minimal UI is enough to validate the architecture.

3. **Per-user brain preferences.** Currently the brain is selected per-message (triage) or per-conversation (sticky). Do we also support per-user `preferences.yaml` overriding the default routing? Useful for shared instances. Defer to 0.3.x.

4. **Tool access in non-Claude brains.** Codex, Gemini, OpenCode all have their own tool ecosystems. The gateway runs them with their default tools. Do we ship a common JC tool surface (memory search, worker spawn, telegram reply) that's exposed to all brains? This is the multi-brain holy grail. Likely 0.4.0+.

5. **Triage budget.** Should we cap how often triage runs (e.g., one classification per 30 seconds for the same user)? Today's projection is negligible cost, but a misbehaving channel firing 100 events/sec would matter.

6. **Memory updates from gateway brains.** When Opus does deep analysis and the result should update an L2 memory entry, who writes? Today Rachel writes via the memory tool when Claude is running interactively. The gateway model invokes brains non-interactively — they need explicit tool affordances. Concrete plan: brain replies can include a `<memory_update slug="x">...</memory_update>` block; the gateway parses and writes via `lib/memory/db.py`. Spec the contract in 0.3.1.

7. **Concurrency model.** Multiple inbound messages arrive while a brain invocation is in-flight — do we serialize per-user (current proposal) or allow parallel brain calls? Per-user serialization keeps conversations coherent. Cross-user parallelism is fine. The queue worker uses an asyncio semaphore per `(channel, user_id)`.

---

## 10. Appendix: Concrete Data Flows

### 10.1 Inbound Telegram message

1. `lib/gateway/channels/telegram.py` long-poll receives `{"chat":{"id":28547271}, "text":"compare three esim providers", "message_id":4830}`.
2. Channel `emit()` puts an Event into the queue with `source="telegram"`, `source_message_id="4830"`, `user_id="28547271"`, `content="compare three esim providers"`.
3. Queue worker picks it up. Acquires per-user semaphore.
4. `sessions.get_active("telegram", "28547271")` → not sticky (idle elapsed). Triage runs.
5. Triage backend (openrouter) returns `{"class":"analysis","brain":"claude:opus-4-7-1m","confidence":0.91}`.
6. Router returns `claude:opus-4-7-1m`.
7. `lib/gateway/brains/claude.py` invokes `claude -p --model opus-4-7-1m --resume <last_session_for_telegram_28547271_opus>`. Stdin: full message + context.
8. Brain returns response. Gateway captures new session id via `_capture_session_id`.
9. `sessions.record_response("telegram", "28547271", "claude:opus-4-7-1m", new_session_id)` — extends sticky window.
10. `lib/gateway/channels/telegram.py.outbound()` posts the response back to chat 28547271 via Bot API.
11. Event status → `done`.

### 10.2 Worker completion

1. `bin/jc-workers` finishes a worker. Calls `mark_terminal()`. Then writes:
   ```json
   // state/events/18-completed.json
   {
     "event_type": "worker.completed",
     "worker_id": 18,
     "topic": "fix bugs",
     "status": "done",
     "duration_seconds": 145,
     "result_path": "state/workers/18/result"
   }
   ```
2. `lib/gateway/channels/jc-events.py` inotify wakes; reads file; emits Event with `source="jc-events"`, `content="worker #18 'fix bugs' completed (done, 145s). Result at state/workers/18/result. Synthesize a summary for the user via telegram."`. Deletes the event file.
3. Queue worker picks up. Triage classifies as `system` → routes to Haiku.
4. Haiku Claude runs with the result file readable. Replies with synthesis.
5. Channel `outbound` for the originating Telegram chat (looked up from the worker's `notify_chat_id` in `state/workers.db`) sends the synthesis. Single message. Done.

### 10.3 Brain switch mid-conversation

1. User on a sticky `claude:sonnet-4-6` thread types `[opus] explain quantum tunneling`.
2. Gateway's input parser strips the `[opus]` prefix and sets `event.meta.brain_override="claude:opus-4-7-1m"`.
3. Router honors the override, skips triage and sticky.
4. Opus replies. `sessions.record_response` updates sticky to opus.
5. Subsequent messages stick to opus until idle timeout.

---

## 11. Out-of-Scope / Future

- Voice as transparent transformer (current proposal: paired channel; defer transparent model to 0.4.0).
- iMessage channel (the official plugin exists; 0.3.x port).
- WhatsApp channel (no MCP plugin exists; 0.4.0+ if demand).
- Multi-tenant SaaS hosting model (architectural foundation laid by 0.3.0; concrete implementation 0.5.0+).
- Brain ensemble / consensus (route the same message to two brains, pick best response). Interesting; not now.
- Cost-aware routing: select cheapest brain that meets a quality bar measured by triage confidence. Likely 0.4.0.

---

## 12. Decision Log

- 2026-04-25 — Spec authored. Branch: `0.3.0-unified-gateway`.
- (open) — Spec review by Luca.
- (open) — Sprint 1 kickoff date.
