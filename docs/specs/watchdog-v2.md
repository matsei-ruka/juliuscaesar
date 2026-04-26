# Watchdog v2 — Generic Process Supervisor

**Status:** Spec (needs review)
**Author:** Rachel
**Date:** 2026-04-26

## Goal

Replace the single-purpose claude-session babysitter at `lib/watchdog/watchdog.sh` with a generic supervisor that watches every long-running JC process (claude session screen, gateway daemon, telegram bun plugin, future event writers). One control plane, uniform restart/backoff/alert policy, and a `jc watchdog` CLI for operator visibility. Stability data motivating this: 14 daemon restarts in 18.6h, all human-driven; the existing watcher had no idea the daemon existed.

## Scope

**In scope — supervised children:**
- `claude-session` — screen-detached `claude --resume <id> --channels plugin:telegram@…` (current sole concern of v1).
- `jc-gateway` — Python daemon (`lib/gateway/runtime.py` via `jc gateway start`).
- `jc-triage-plugin` — `bun server.ts` spawned by Claude Code's telegram plugin (currently watched indirectly through `telegram_plugin_owned_by_us` heuristics; promote to first-class child).
- `jc-events` (future) — long-running event writers (e.g., gmail poller, calendar poller) once they exist as standalone daemons.

**Out of scope — NOT watched:**
- `jc workers` background agents — owned by the workers subsystem; they self-report completion via Telegram and are short-lived by design.
- Adapters spawned by the gateway dispatcher (`claude -p`, openrouter HTTP calls) — gateway-managed, per-event, transient. The gateway is responsible for adapter lifecycle and self-heal (see `self-heal-recovery.md`).
- One-shot heartbeat tasks (cron-driven, non-resident).

The line: a process is a supervisor child iff it is expected to be resident continuously for the lifetime of the instance and its death blocks user-facing functionality.

## Architecture

### Components

```
lib/watchdog/
  supervisor.py        # main loop; reads registry; ticks every TICK_SECONDS
  registry.py          # loads ops/watchdog.yaml → list[ChildSpec]
  child.py             # ChildSpec, ChildState; per-child operations
  health.py            # checks: pid-alive, cwd-match, heartbeat-file, http-probe
  policy.py            # backoff calculator, alert-mode trigger
  cli.py               # `jc watchdog status|tail|reset|reload`
  watchdog.sh          # thin shell entrypoint kept for cron compatibility
```

The supervisor is a single Python process started under cron (every minute) **or** as a long-lived `screen` session (preferred once stable). Cron mode is idempotent: each tick acquires `state/watchdog/lock` (flock, non-blocking) and exits if held.

### Child registry

`ops/watchdog.yaml` (per-instance, replaces flat `ops/watchdog.conf`):

```yaml
children:
  - name: claude-session
    type: screen
    screen_name: jc-rachel
    start: lib/watchdog/start-claude.sh
    health:
      cwd_match: $INSTANCE_DIR
      proc_match: "claude .*--channels plugin:telegram"
    restart:
      backoff: [5, 10, 30, 60, 300]
      max_in_window: 5
      window_seconds: 600

  - name: jc-gateway
    type: daemon
    start: jc gateway start --foreground
    pidfile: state/gateway/daemon.pid
    health:
      heartbeat_file: state/gateway/heartbeat
      heartbeat_max_age_seconds: 30
    restart: { backoff: [5, 10, 30, 60, 300], max_in_window: 5, window_seconds: 600 }

  - name: jc-triage-plugin
    type: managed-by
    parent: claude-session
    pidfile: ~/.claude/channels/telegram/bot.pid
    health: { pid_alive: true, parent_descendant: claude-session }
    # No independent start — restarting parent respawns this.
    on_dead: restart-parent
```

`type` values:
- `screen` — start command runs inside a named GNU screen session; liveness = screen exists + matching child process inside.
- `daemon` — start command forks a long-running process; liveness = pidfile valid + heartbeat fresh.
- `managed-by` — child is owned by another supervisor child (e.g., bun plugin owned by claude session); the supervisor does not restart it directly, only flags the owning parent for restart.

### Per-tick loop

```
for child in registry.load():
    state = state_store.get(child.name)
    if alert_mode(state): skip
    healthy = health.check(child)
    if healthy:
        state.consecutive_failures = 0; continue
    if not policy.may_restart(child, state):
        alert(child, state); state.alert_mode = True; continue
    delay = policy.backoff_for(state.consecutive_failures)
    if state.last_attempt_age < delay: continue
    restart(child); state.consecutive_failures += 1; state.last_attempt = now
```

State persisted to `state/watchdog/state.json` so restarts of the supervisor itself preserve counters.

## Health checks

A child is alive iff **all** of its declared probes pass. Available probes:

- **pid_alive** — pid from pidfile (or pgrep) responds to `kill -0`.
- **cwd_match** — `/proc/<pid>/cwd` resolves to the instance dir. Required for any process that can be confused with another instance on the same host (claude, gateway).
- **proc_match** — argv regex match. Used together with cwd_match when no pidfile exists.
- **heartbeat_file** — file mtime within `heartbeat_max_age_seconds` of now.
- **http_probe** — GET `url` returns 2xx within `timeout_seconds`. Reserved for future HTTP daemons; not used in v1 children.
- **parent_descendant** — pid's process ancestry includes the named parent child's pid (kept from current `pid_descends_from_any`).

### Heartbeat contract for the gateway

The gateway must touch `state/gateway/heartbeat` at the bottom of each poll loop iteration (≤ 5s under normal load). Add to `GatewayRuntime.run()`:

```python
def _touch_heartbeat(self) -> None:
    self._heartbeat_path.touch(exist_ok=True)
```

If the gateway is mid-dispatch (adapter call may take minutes), the heartbeat is updated by a separate ticker thread, not the main loop. This is the only way to distinguish "gateway is doing real work" from "gateway is wedged in a deadlock during shutdown" — which is exactly the pain point that caused 14 manual restarts.

## Restart policy

Per-child config (defaults shown), driven by `policy.py`:

- **Exponential backoff** between restart attempts: `5s → 10s → 30s → 60s → 300s` cap. Resets to 5s after one full health-check tick succeeds.
- **Restart budget**: at most 5 restarts within a 10-minute sliding window. The 6th triggers **alert mode**.
- **Alert mode**: supervisor stops attempting to restart the child, sends one Telegram alert through the existing `heartbeat/lib/send_telegram.sh` sender ("⚠️ jc-gateway restart loop — 5 restarts in 10m, last failure: <stderr tail>"), and waits for `jc watchdog reset <child>` from the operator before resuming.
- **Restart command failures** (non-zero rc from `start`) count toward the budget.

Backoff is per-child, not global: the gateway flapping does not delay claude-session restart.

## Operator UX

`jc watchdog status` — table output:

```
NAME              PID     UPTIME   RESTARTS  LAST FAILURE        MODE
claude-session    18472   2h13m    0         —                   ok
jc-gateway        19014   18m      3         pid 18901 vanished  backoff
jc-triage-plugin  19077   18m      0         —                   ok
```

`jc watchdog tail <child>` — `tail -f` of the child's log (per-child log file under `state/watchdog/logs/<child>.log`; supervisor multiplexes start-script stdout/stderr).

`jc watchdog reset <child>` — clear alert mode and restart counters; supervisor resumes management on the next tick.

`jc watchdog reload` — re-read `ops/watchdog.yaml`; new children start, removed children stop, modified children get re-applied (restart command changes take effect on next restart, not immediately).

JSON output via `--json` for scripts.

## Migration

Current state: `lib/watchdog/watchdog.sh` is a 438-line bash script that watches one screen, with implicit secondary care for the bun plugin via `telegram_plugin_owned_by_us`. `ops/watchdog.conf` is a flat shell sourced for `SESSION_ID`, `SCREEN_NAME`, `CLAUDE_ARGS_EXTRA`.

Cutover plan:

1. **Ship v2 alongside v1.** Add `lib/watchdog/supervisor.py` and `ops/watchdog.yaml` template. Cron continues calling `watchdog.sh` (v1) until the operator opts in.
2. **Operator opts in.** `jc watchdog migrate` reads the existing `ops/watchdog.conf`, generates an equivalent `ops/watchdog.yaml` with one `claude-session` child + one `jc-triage-plugin` managed-by child, and rewrites the cron entry to invoke `supervisor.py` instead of `watchdog.sh`.
3. **Two weeks of dual-availability.** Either entrypoint works; `ops/watchdog.conf` keys are aliased into the registry (e.g., `SESSION_ID` → `claude-session.start` arg). Operator can add `jc-gateway` as a second child once v2 has soaked.
4. **Deprecate.** `watchdog.sh` becomes a thin shim that execs `supervisor.py` with a synthetic single-child registry. Drop the .conf alias path.

The bun-plugin asymmetric-state cleanup logic in v1 (`telegram_plugin_related_pids`, kill stale PIDs before parent restart) moves into `child.py` for `managed-by` children with `on_dead: restart-parent`. It is not lost.

## Open questions

- **Supervisor lifecycle.** Cron-driven (current model, simpler) or self-supervised long-running screen (lower latency, but who watches the watcher)? Proposal: ship cron-driven; revisit once v2 is stable.
- **Heartbeat thread vs. async loop.** Gateway is currently sync threading. Adding a heartbeat-only ticker thread is trivial; doing it correctly across a future asyncio rewrite is more work. Worth deferring to whichever rewrite lands first?
- **Alert channel.** Today: Telegram only. Should alert mode also write to systemd-journal / a file the operator's monitoring scrapes? Probably yes for prod instances, but not blocking v1.
- **State store format.** JSON file is fine for ≤10 children. SQLite if we ever supervise dozens (we won't on a single instance).
- **Health check on first tick after start.** If a child needs 10s to come up, the first health check fails and we restart immediately. Add a configurable `start_grace_seconds` (default 15) before the first check counts.
- **Is the gateway daemon actually deadlocking on shutdown, or is it blocking on a long adapter call?** The 14 restarts could be either; a heartbeat from the dispatcher itself (not just the poll loop) would distinguish. Worth confirming before relying on heartbeat-mtime as the sole liveness signal.
