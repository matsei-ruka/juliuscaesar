# Claude Warm Pool — Persistent REPL Worker Pool

**Status:** Spec (needs review)
**Author:** Rachel
**Date:** 2026-04-26

## Goal

The gateway dispatcher spawns a fresh `claude -p` process per event — a 3-8s cold-start overhead (loading CLAUDE.md, MCP config, tools, session resume). This is the largest single latency source, dominating time-to-first-token. Eliminate it by maintaining a warm pool of persistent `claude` REPL processes, one per sticky `(conversation_id, brain)` pair, reused across events. Each pool member maintains its own session-id across many invocations; new events reuse the same process stdin/stdout pipes. Net effect: ~5-7s per-event latency cut in half.

## Architecture

### Components

```
lib/gateway/
  warm_pool/
    __init__.py
    pool.py              # PoolManager, lifecycle, pool membership
    process.py           # PoolProcess, stdin/stdout protocol, health checks
    protocol.py          # request/response JSON schema
    session_reuse.py     # sticky session-id per pool member
  brains/
    base.py              # (modified) invoke() method uses pool if available
```

### Pool membership

Pool key: `(conversation_id, brain, model)` tuple. Pool member: a `PoolProcess` wrapping a `claude --daemon --stdio` or equivalent long-lived process.

```python
@dataclass
class PoolMember:
    key: tuple[str, str, str | None]      # (conversation_id, brain, model)
    process: subprocess.Popen
    stdin: IO[str]
    stdout: IO[str]
    session_id: str | None                # claude session uuid, if any
    created_at: float
    last_used: float
    message_count: int
    healthy: bool
```

`PoolManager` maintains a dict of pool members, evicts idle members (timeout 300s default), caps total pool size (default 20 processes), and provides `get_or_create(key)` and `release(key)` primitives.

### Protocol

Communication via JSON lines on stdin/stdout. Request envelope:

```json
{
  "type": "invoke",
  "session_id": "<uuid-or-null>",
  "model": "<model-name-or-null>",
  "brain": "<brain-name>",
  "messages": [{"role": "user", "content": "..."}],
  "tools": [{"name": "...", "description": "..."}],
  "temperature": 0.7,
  "max_tokens": 2048
}
```

Response envelope:

```json
{
  "type": "response",
  "session_id": "<uuid>",
  "content": "...",
  "stop_reason": "end_turn|tool_use|max_tokens",
  "usage": {"input_tokens": 1234, "output_tokens": 567},
  "tools_used": []
}
```

On error:

```json
{
  "type": "error",
  "reason": "session_expired|bad_input|timeout|stderr_tail",
  "stderr": "...",
  "session_id_extracted": "<uuid-or-null>"
}
```

The `claude --daemon --stdio` mode is a speculative feature; if it doesn't exist in the Claude CLI version in use, the pool gracefully falls back to `claude -p subprocess` (no change to current behavior). The spec does not require CLI changes; it works with the existing `claude -p` subprocess by wrapping it carefully.

### Lifecycle

1. **Pool creation:** `PoolManager()` initialized with config (max_size, idle_timeout_seconds, startup_grace_seconds).
2. **Get-or-create:** `brain.invoke(event)` calls `pool.get_or_create(key)`. If member exists + healthy: reuse. If missing or dead: spawn new process, warm up (send a no-op heartbeat request), record session-id.
3. **Invoke:** `process.invoke(request)` writes JSON to stdin, reads JSON from stdout, validates response, updates last_used timestamp.
4. **Session reuse:** Response includes `session_id`; store it in `pool_member.session_id`. Next request for the same key includes the stored session-id in the request envelope, so `claude` resumes without `--resume` flag (no CLI arg bloat).
5. **Idle eviction:** Supervisor or background thread ticks every 30s, evicts members with `now - last_used > idle_timeout_seconds`.
6. **Graceful shutdown:** On gateway exit, drain all members (give in-flight requests 5s), then terminate processes.

### Process communication

Wrapped `claude -p` with pipes:

```python
proc = subprocess.Popen(
    [self.claude_bin, "-p", "--model", model, "--no-browser"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,  # line-buffered
)
```

Each invoke: write JSON request line, read JSON response line. If response is incomplete or unparseable, mark unhealthy and close. If stderr has content, log it as a warning and extract session-id from stderr in case of re-auth prompts.

Timeout per request: `adapter_timeout_seconds` (currently 300s in gateway config). If no response within timeout, kill the process and evict.

### Session reuse

Instead of `claude -p --resume <uuid>` every time, pass the session-id in the JSON request envelope:

```json
{"type": "invoke", "session_id": "7d5ec0b5-47a6-4ff3-ae5f-2a6a6657cf46", ...}
```

Claude CLI (via stdin JSON protocol) internally resumes the session if the uuid is valid. On response, extract the (possibly new or rotated) session-id and store it for the next request.

Fallback: if the JSON protocol doesn't support `session_id` field, the pool still works — just loses the session-resume optimization and behaves like repeated `claude -p` calls. Not a blocker.

### Health checks

Pool members are probed on get:
- Process still alive (`kill -0`)?
- Last response was valid JSON?
- No repeated `session_expired` or `bad_input` errors in last N requests?

On health check failure: evict immediately, spawn a fresh replacement.

### Metrics & observability

Track per-pool-member:
- Pool hits vs. misses (useful to decide max_size).
- Message count, uptime, avg latency per request.
- Process exit reason (timeout, error, idle eviction, shutdown).
- Session-id rotation events (when a new session-id appears mid-conversation).

Export via `lib/gateway/metrics.py` (already has `MetricsRecorder`).

Dashboard queries:
- Pool utilization (live members / max_size).
- Hit rate (requests from warm pool / total requests).
- Latency: `invoke_time_warm` vs. `invoke_time_cold` (subprocess spawn).
- Failed resumes (session_expired on warm member = bug signal).

## Implementation phases

### Phase 1 (MVP)

- Pool manager + process wrapper with JSON protocol.
- Integrate into `lib/gateway/brains/base.py:invoke()` with fallback to current subprocess path if pool unavailable.
- No CLI changes; use existing `claude -p` subprocess, just don't kill it after the request.
- Test with small pool (max_size=3).

### Phase 2 (optimization)

- Regex prefilter for trivial requests (don't spend pool slot on single-emoji or `/cmd`).
- Background idle eviction thread.
- Metrics collection.

### Phase 3 (advanced)

- CLI `--daemon --stdio` mode (if Claude adds it) for true REPL semantics.
- Per-brain subconfiguration (e.g., opus pool size different from haiku).
- Pool warming heuristics (pre-spawn likely hot conversations on gateway startup).

## Edge cases

- **Process death during in-flight request.** Stdin write succeeds, but stdout read hangs or EOF. Timeout fires after 300s, process is killed, member evicted. Upstream re-enqueues the event.
- **Session-id rotation mid-conversation.** Claude may rotate the session-id on re-auth or subscription change. Pool stores the new one; next invoke uses the rotated id. No loss of context.
- **JSON framing corruption (partial line in stdout buffer).** Timeout fires; member marked unhealthy; evicted. Rare in practice.
- **Adapter returns `session_expired` while in the pool.** Member stays healthy for future attempts (auth may recover). Upstream classifier branches to login-recovery flow (from `self-heal-recovery.md`); on successful re-auth, the same pool member is reused with the new session-id.
- **Adapter returns `bad_input` repeatedly from the same member.** After 3 consecutive `bad_input` errors, member is marked unhealthy and evicted. Next request spawns fresh. Guards against stuck error states.
- **Pool member uptime > session max-lifetime (unlikely but possible).** If Claude session has a server-side TTL (e.g., 30 days), the pool member eventually fails with `session_expired`. Handled by the classifier + login-recovery flow.
- **Graceful shutdown with in-flight requests.** Gateway stop signal drains: wait up to 5s for all in-flight requests to finish. After 5s, SIGTERM all remaining processes. After another 2s, SIGKILL.
- **Conversation_id recycled across users (shouldn't happen, but if it does).** Pool key includes conversation_id, so a new user reusing an old id gets a fresh pool member. Sticky session-id from the old user is not reused.

## Rollback & safety

- Current subprocess adapter path is left intact. Pool is optional — if pool unavailable or unhealthy, invoke falls back to `claude -p subprocess` (one-line in the dispatcher).
- Config flag `enable_warm_pool: true` (default false initially). Operators can opt in after a stabilization period.
- If pool causes latency regression (unlikely), set `enable_warm_pool: false` and restart gateway. No code changes needed.

## Open questions

- **`claude -p` vs. `claude --daemon --stdio`.** Does the CLI support a daemon mode with JSON stdin/stdout protocol? Proposal: implement with `claude -p subprocess` first (just don't kill), then add daemon mode as an optimization if the CLI feature lands.
- **Session-id in JSON protocol.** Does Claude CLI (via stdin) support a `session_id` field in the request envelope, or does it only support `--resume <uuid>` as a flag? If not, the pool can still work — it just re-authenticates more often. Worth confirming before implementation.
- **Per-model pool segregation.** Should `(conversation_id, brain, model=None)` be the pool key, or just `(conversation_id, brain)` and let the request envelope override the model per-invoke? Current proposal: include model in the key, so `opus` and `sonnet` requests for the same conversation go to different pool members. Simpler and cleaner.
- **Max pool size tuning.** Default 20 assumes ≤20 concurrent conversations. If an instance scales to 100 concurrent conversations, 20 processes × 400MB/process = 8GB memory used. Monitor and add a config to scale pool size vs. memory budget?
- **Pre-warming on startup.** Gateway boot time could spawn N pool members for common conversations (e.g., top 5 recent conversations from `sessions.db`). Worth it if gateway is restarted often; not worth if restarts are rare. Punt for now.
- **Pool member logging level.** Lots of opportunity for verbose logging (every invoke, every session-id rotation). Propose: log only errors and state transitions; metrics collection handles aggregates.
