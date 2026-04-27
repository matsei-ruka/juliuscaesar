# Process hygiene spec

Prevent orphan Claude (brain) and adapter processes left running when one half of the pair crashes, hangs, or is killed.

## Problem

When an adapter (e.g., TelegramChannel poller) exits unexpectedly:
- Brain process (e.g., Claude) continues running indefinitely
- State files are stale; next watchdog restart re-spawns a new brain
- Multiple Claude instances accumulate, consuming resources and tokens

When a brain exits (e.g., Claude crashes):
- Adapter continues polling, blocking on dead brain replies
- Adapter hogs resources waiting for responses that never come

## Solution

### 1. Session registry (`state/sessions.json`)

Track active session metadata: PID, brain, adapter, start time, last heartbeat.

```json
{
  "active": [
    {
      "session_id": "uuid",
      "brain_pid": 1234,
      "adapter": "telegram",
      "started_at": "2026-04-28T10:00:00Z",
      "last_heartbeat": "2026-04-28T10:05:15Z"
    }
  ]
}
```

- Written by `GatewayRuntime.__init__()` with UUID + timestamp
- Updated on heartbeat (every ~5s from runtime ticker)
- Checked by watchdog's `jc watchdog status` and orphan-reaper

### 2. Adapter-brain coupling

Adapter process (gateway runtime) spawns brain (Claude) as a subprocess:
- Adapter is parent; brain is child
- On adapter exit (normal or crash) → OS sends SIGKILL to brain (if parent dies)
- If brain dies → adapter detects exit status, logs, and terminates (fail-fast)

Currently: `GatewayRuntime` forks brain inline. Change to:
```python
# Pseudo-code
self.brain_proc = Popen([brain_cmd], ...)
try:
    ... main loop ...
finally:
    if self.brain_proc and self.brain_proc.poll() is None:
        self.brain_proc.terminate()
        self.brain_proc.wait(timeout=5)
```

### 3. Watchdog orphan reaper

`jc watchdog status` detects:
- Processes in session registry with PID not alive
- Processes older than 1 hour with no heartbeat update (stale)

Action:
```
jc watchdog reap
```

Manually or automatically:
- Delete stale entries from session registry
- Kill orphan brain PIDs (signal + wait + force kill)
- Log action for audit

### 4. Graceful shutdown signals

When adapter receives SIGTERM / SIGINT:
- Flush any pending queue items
- Send SIGTERM to brain
- Wait 5 seconds
- Force kill if needed (SIGKILL)
- Update session registry (mark inactive)

## Implementation phases

### Phase 1 (this PR): session registry + watchdog status

- Add `sessions.json` writer to `GatewayRuntime`
- Add `jc watchdog status --sessions` to show active/stale
- No automatic reaping yet

### Phase 2: adapter-brain coupling + fail-fast

- Modify `GatewayRuntime` to spawn brain as subprocess
- Add brain health check (detect exit, restart or fail)
- Implement graceful shutdown signal handling

### Phase 3: automatic reaper

- Add `jc watchdog reap` command
- Optional: auto-reap on watchdog tick if stale > N minutes

## Files to add/modify

- `lib/gateway/sessions.py` — session registry CRUD
- `lib/gateway/runtime.py` — write session on init, heartbeat, shutdown
- `bin/jc-watchdog` — add `status --sessions` and `reap` subcommands
- `lib/watchdog/cli.py` — implement reaper

## Risks & mitigations

- **Risk:** Killing wrong process (PID reuse after crash).
  - **Mitigation:** Check process age, command line, working directory before kill.
- **Risk:** Zombie brain processes (orphaned, unresponsive).
  - **Mitigation:** Use SIGKILL after short timeout; don't leave zombies.

## Testing

Unit tests:
- Session registry read/write
- Stale detection logic
- Process lookup (pid_alive, cwd_match)

Integration tests:
- Simulate adapter crash, verify brain cleanup
- Simulate brain exit, verify adapter detects and logs
