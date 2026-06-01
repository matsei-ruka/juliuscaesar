# Doctor PID liveness

## Problem

On 2026-05-29 14:00:41Z, Ethan Zhang's gateway daemon was stopped cleanly
and stayed dead for four days. During that window, `jc doctor` was run
multiple times on `192.168.14.115` and reported "All critical checks
passed". The check that existed (`Gateway` section) inspected
`state/gateway/queue.db`, `ops/gateway.yaml`, and the existence of
`state/gateway/jc-gateway.pid` — none of which catch a dead daemon. The
pidfile was orphaned but present; doctor read it, found no live PID, and
silently downgraded the finding to a `warn` line in a section operators
weren't watching.

Three failure modes need to become CRITICAL (i.e. non-zero exit):

1. `state/gateway/jc-gateway.pid` exists but the PID is not alive.
2. The PID is alive but its `/proc/<pid>/cmdline` does not contain
   `jc-gateway` (foreign process recycled the PID, or operator started
   something else under the same pidfile).
3. `state/supervisor/jc-supervisor.pid` exhibits the same conditions.

A fourth, lower-severity check captures the cross-instance bot-token
collision the same incident exposed (`HOT.md` "CRITICAL env-leak" line):

4. Recent gateway log lines contain `telegram poll error: HTTP Error 409`
   → WARN, "cross-instance bot-token contention detected — sibling
   instance polling same bot".

## Solution

A new module `lib/gateway/liveness.py` exposes three pure helpers used by
both `bin/jc-doctor` and the new pytest suite:

```python
def gateway_pid_finding(instance_dir: Path) -> Finding: ...
def supervisor_pid_finding(instance_dir: Path) -> Finding: ...
def telegram_409_finding(instance_dir: Path, *, tail_lines: int = 200) -> Finding | None: ...
```

`Finding` reuses the dataclass shape already in
`lib/gateway/codex_diagnostics.py` (`level`, `message`). Levels are
`ok`, `warn`, `info`, `fail`.

### PID-liveness algorithm

For each pidfile:

1. If pidfile is absent → `info` ("daemon stopped"). Not a failure.
2. Read PID; if unparseable → `fail` ("pidfile corrupt").
3. `os.kill(pid, 0)`:
   - `ProcessLookupError` → `fail` ("pidfile present but PID dead").
   - `PermissionError` → process exists under another user; treat as
     alive for the liveness purpose, but check cmdline.
   - Otherwise (no exception) → alive.
4. Read `/proc/<pid>/cmdline`:
   - If empty (race: process exited between calls) → `fail` ("PID dead").
   - If marker (`jc-gateway` / `jc-supervisor`) absent → `fail` ("PID
     belongs to a different process").
   - Otherwise → `ok`.

`/proc` is Linux-only; on platforms without `/proc` the cmdline check
short-circuits to `ok` after the `os.kill(pid, 0)` succeeds. JC fleets
are Linux-only today; the macOS dev case stays best-effort.

### 409 log scan

Read the last `tail_lines` (default 200) of
`state/gateway/gateway.log`. If any contains the literal `HTTP Error
409: Conflict` (case-insensitive substring), return a single WARN with
the most-recent offending timestamp prefix. Multiple matches collapse to
one finding so doctor output stays scannable.

### Doctor wiring

In `bin/jc-doctor`'s `Gateway` section, replace the existing
`GATEWAY_PIDFILE` block with a Python embed that calls all three helpers
and emits the standard `OK:` / `WARN:` / `FAIL:` prefixes the bash
parser already understands. Any `fail` level increments the existing
`FAIL` counter → non-zero exit. The supervisor PID check joins the same
section.

### Acceptance

1. With a fresh instance whose gateway is running, doctor remains green
   for the new checks.
2. With a stale pidfile pointing at a dead PID, doctor exits non-zero
   and prints `✗ gateway pidfile present but PID dead`.
3. With a live PID whose cmdline does not match, doctor exits non-zero
   with `✗ gateway PID belongs to a different process`.
4. With a `gateway.log` containing a recent `HTTP Error 409: Conflict`
   line, doctor prints `! 409 conflict detected` and stays non-fatal
   (does not fail the run).
5. Unit tests cover all five paths (dead PID, foreign cmdline, fresh
   PID, missing pidfile, 409 in log).
