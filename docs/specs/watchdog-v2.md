# Watchdog v2 — Generic Process Supervisor

**Status:** Spec (needs review)
**Author:** Rachel
**Date:** 2026-04-26

## Goal

Replace the single-purpose script at `lib/watchdog/watchdog.sh` with a generic supervisor that watches every long-running JC process under one control plane. In gateway mode (the supported runtime as of 0.3.0), the only resident JC process is `jc-gateway`; the legacy claude+screen path is deprecated and slated for removal in 0.5.0. Stability data motivating the rewrite: 14 daemon restarts in 18.6h, all human-driven, because the existing script doesn't supervise the daemon directly — it only flips into a `main_gateway` branch that polls a pidfile and restarts on death, with no backoff, no alerting threshold, no per-child state, and no knowledge of when the daemon is wedged vs. doing work.

## Scope

**In scope — supervised children (gateway runtime):**
- `jc-gateway` — Python daemon (`lib/gateway/runtime.py` via `jc gateway start`). The single resident process for normal operation.
- Future event writers (`jc-events` pollers — gmail, calendar) once they exist as standalone daemons, not as gateway-internal threads.

**Legacy children (deprecated, kept until 0.5.0 cutoff):**
- `claude-session` — screen-detached interactive `claude` session used by pre-0.3.0 instances. Supported via a `legacy-claude` child type for backward compat; emits a deprecation warning on every tick. The bun telegram plugin and its asymmetric-state cleanup live inside this child type, not as their own entry, since neither exists in gateway mode.

**Out of scope — NOT watched:**
- `claude -p` adapter invocations spawned by the gateway dispatcher per event — gateway-managed, transient. Lifecycle and self-heal live in `self-heal-recovery.md`.
- `jc workers` background agents — owned by the workers subsystem; short-lived and self-report on completion.
- One-shot heartbeat / cron tasks.

The line: a process is a supervisor child iff it must be resident continuously and its death blocks user-facing functionality. In gateway mode that set is `{jc-gateway}` plus, eventually, standalone event writers.

## Architecture

### Components

```
lib/watchdog/
  supervisor.py        # main loop; reads registry; ticks every TICK_SECONDS
  registry.py          # loads ops/watchdog.yaml → list[ChildSpec]
  child.py             # ChildSpec, ChildState; per-child operations
  health.py            # checks: pid-alive, cwd-match, heartbeat-file
  policy.py            # backoff calculator, alert-mode trigger
  cli.py               # `jc watchdog status|tail|reset|reload`
  watchdog.sh          # thin shell entrypoint kept for cron compatibility
```

The supervisor is a Python process invoked from cron (every minute) and is idempotent: each tick takes `state/watchdog/lock` (`flock -n`) and exits if held. Long-lived screen mode is **not** introduced — gateway already does the long-running work and the supervisor needs to stay cheap and crash-safe.

### Child registry

`ops/watchdog.yaml` (per-instance, replaces flat `ops/watchdog.conf`):

```yaml
children:
  - name: jc-gateway
    type: daemon
    start: jc gateway start --foreground
    pidfile: state/gateway/daemon.pid
    health:
      pid_alive: true
      cwd_match: $INSTANCE_DIR
      heartbeat_file: state/gateway/heartbeat
      heartbeat_max_age_seconds: 30
    restart:
      backoff: [5, 10, 30, 60, 300]
      max_in_window: 5
      window_seconds: 600
      start_grace_seconds: 15

  # Legacy entry — only present on instances not yet migrated to gateway mode.
  # The migration helper (`jc migrate-to-0.3`) removes this child and adds
  # `jc-gateway` above. Generates a deprecation log line on every tick.
  - name: claude-session
    type: legacy-claude
    enabled: false
    screen_name: jc-rachel
    session_id: <uuid-or-empty>
    health:
      cwd_match: $INSTANCE_DIR
      proc_match: "claude .*--channels plugin:telegram"
    restart: { backoff: [5, 10, 30, 60, 300], max_in_window: 5, window_seconds: 600 }
```

`type` values:
- `daemon` — start command forks a long-running process; liveness = pidfile valid + cwd-match + heartbeat fresh.
- `legacy-claude` — wraps the existing `main()` path from `watchdog.sh` (screen + claude + bun-plugin asymmetric-state recovery). Encapsulates the deprecation, including the orphan/foreign telegram plugin cleanup. No new features land here.
- `http-daemon` (reserved) — future child type for HTTP-probed services. Not used in v1.

### Per-tick loop

```
acquire flock or exit
for child in registry.load_enabled():
    state = state_store.get(child.name)
    if state.alert_mode: continue            # waiting for `jc watchdog reset`
    if health.check(child): state.consecutive_failures = 0; continue
    if not policy.may_restart(state): alert(child, state); state.alert_mode=True; continue
    delay = policy.backoff_for(state.consecutive_failures)
    if state.last_attempt_age < delay: continue
    rc = child.start()
    state.last_attempt = now
    state.consecutive_failures += 1
    state.attempts_in_window.append(now)
state_store.save()
```

State persisted to `state/watchdog/state.json` so the supervisor's own restart preserves counters.

## Health checks

A child is alive iff **every** declared probe passes. Available probes:

- **pid_alive** — pid from pidfile responds to `kill -0`.
- **cwd_match** — `/proc/<pid>/cwd` resolves to the instance dir. Mandatory for any process that can be confused with another instance on the same host (gateway, legacy-claude).
- **proc_match** — argv regex match. Used together with cwd_match when no pidfile exists (legacy-claude).
- **heartbeat_file** — file mtime within `heartbeat_max_age_seconds` of now.
- **parent_descendant** — pid's process ancestry includes the named parent child's pid (kept for legacy-claude's bun plugin handling, internal to that child type).

### Heartbeat contract for the gateway

The gateway must touch `state/gateway/heartbeat` at the bottom of each poll loop iteration (≤ 5s under normal load). Add to `GatewayRuntime`:

```python
def _touch_heartbeat(self) -> None:
    self._heartbeat_path.touch(exist_ok=True)
```

Adapter calls can take minutes, so the heartbeat is updated by a separate ticker thread (started in `GatewayRuntime.run()`, joined on shutdown), not the main loop. This is the only way to distinguish "gateway is doing real work" from "gateway is wedged in the documented `jc gateway restart` deadlock during shutdown" — the exact failure mode that drove the 14 manual restarts.

## Restart policy

Per-child (defaults shown), driven by `policy.py`:

- **Exponential backoff** between restart attempts: `5s → 10s → 30s → 60s → 300s` cap. Resets to 5s after one full health-check tick succeeds.
- **Restart budget**: ≤ 5 restarts within a 10-minute sliding window. The 6th flips the child to **alert mode**.
- **Alert mode**: supervisor stops attempting to restart, sends one Telegram alert via `heartbeat/lib/send_telegram.sh` (`"⚠️ jc-gateway restart loop — 5 restarts in 10m, last failure: <stderr tail>"`), and waits for `jc watchdog reset <child>` from the operator before resuming.
- **Start-grace window**: `start_grace_seconds` (default 15) suppresses health-check failures immediately after a start attempt; covers daemon boot time so we don't misclassify a slow start as a crash.
- **Restart command failures** (non-zero rc from `start`) count toward the budget.

Backoff is per-child, not global: legacy-claude flapping does not delay gateway restart.

## Operator UX

`jc watchdog status` — table output:

```
NAME              PID     UPTIME   RESTARTS  LAST FAILURE                MODE
jc-gateway        19014   18m      3         heartbeat stale (>30s)      backoff
claude-session    18472   2h13m    0         —                           ok (legacy)
```

`jc watchdog tail <child>` — `tail -f` of the child's log (per-child file under `state/watchdog/logs/<child>.log`; supervisor multiplexes start-script stdout/stderr).

`jc watchdog reset <child>` — clear alert mode and restart counters; supervisor resumes management on the next tick.

`jc watchdog reload` — re-read `ops/watchdog.yaml`; new children start, removed children stop, modified children get re-applied (restart-command changes take effect on the next restart, not immediately).

`--json` flag for scriptable output.

## Migration

Current state: `lib/watchdog/watchdog.sh` is a 438-line bash script with two top-level branches — `main_gateway` (gateway mode, what Rachel runs today) and `main` (legacy claude+screen+bun-plugin path, with its own deprecation warning). `ops/watchdog.conf` is a flat shell sourced for `RUNTIME_MODE`, `SESSION_ID`, `SCREEN_NAME`, `CLAUDE_ARGS_EXTRA`.

Cutover plan:

1. **Ship v2 alongside v1.** Add `lib/watchdog/supervisor.py` and `ops/watchdog.yaml` template. Cron continues calling `watchdog.sh` until the operator opts in.
2. **Operator opts in.** `jc watchdog migrate` reads `ops/watchdog.conf`, generates an equivalent `ops/watchdog.yaml`:
   - `RUNTIME_MODE=gateway` → single `jc-gateway` child.
   - `RUNTIME_MODE=legacy-claude` → single `claude-session` child of type `legacy-claude` carrying `SESSION_ID`/`SCREEN_NAME`/`CLAUDE_ARGS_EXTRA`.
   Then rewrites the cron entry to invoke `supervisor.py`.
3. **Two-week dual-availability.** Either entrypoint works; `ops/watchdog.conf` keys are aliased into the registry on read. Operator can add new children (event writers) once v2 has soaked.
4. **Deprecate the shim.** `watchdog.sh` becomes a thin wrapper that execs `supervisor.py` with a synthetic single-child registry. Drop the `.conf` alias path.
5. **0.5.0 — drop legacy-claude.** Remove the child type along with the screen/bun-plugin code paths.

The bun-plugin asymmetric-state cleanup logic (kill orphan plugin before parent restart, kill stale claude before respawn) lives entirely inside the `legacy-claude` child type, not in the generic supervisor — it has no analogue in gateway mode.

## Open questions

- **Heartbeat thread vs. async loop.** Gateway is sync threading today. Adding a heartbeat-only ticker thread is trivial; the question is whether to do it as a stand-alone fix now or roll it into a broader asyncio rewrite later. Proposal: ship the thread now — the 14 manual restarts are happening today.
- **Alert channel.** Telegram only at v1. Should alert mode also write to systemd-journal / a file the operator's monitoring scrapes? Yes for prod instances eventually, not blocking v1.
- **State-store format.** JSON file is fine for ≤ 10 children. SQLite if we ever supervise dozens (we won't on a single instance).
- **Is `jc gateway restart` deadlocking on shutdown, or blocking on a long adapter call?** The 14 restarts could be either; a heartbeat from the dispatcher itself (not just the poll loop) would distinguish. Worth confirming before relying on heartbeat-mtime as the sole liveness signal.
- **Adapter-failure session-recovery overlap.** When `claude -p --resume <id>` returns "session not found", today the gateway retries blindly. The fix lives in `self-heal-recovery.md` (gateway-side, classifier branch `session_missing`), not in the watchdog — but the supervisor needs to NOT restart the gateway just because adapter calls are failing. Health stays green if the gateway process is still polling the queue, even when every dispatch returns rc=1.
