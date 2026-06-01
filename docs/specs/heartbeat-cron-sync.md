# Heartbeat cron auto-sync

## Problem

`heartbeat/tasks.yaml` carries an `enabled:` flag per task, but the actual
trigger lives in the operator's crontab. The two drift silently:

- Mikaela's instance had `dream_tick` and `self_model_run` flipped to
  `enabled: true` on 2026-05-17, but no cron entries were ever installed.
  Both builtins sat dormant for two weeks before the gap surfaced.
- Conversely, an operator can disable a task in `tasks.yaml` and forget to
  delete the corresponding cron line. The job keeps firing, hits the
  builtin's auto-dry-run guard, and silently writes nothing — but still
  burns the schedule and the lock file.

The cause is operator memory: the same fact (this task should run at this
cadence) lives in two places. We collapse it into one and let the framework
reconcile.

## Solution

`tasks.yaml` becomes the source of truth. Each task can carry a `schedule:`
field (a standard 5-field cron expression). A new `jc heartbeat cron`
subcommand reads the file and writes the calling user's crontab to match.

### YAML extension

```yaml
tasks:
  dream_tick:
    builtin: dream_tick
    enabled: true
    schedule: "30 3 * * *"          # nightly at 03:30 instance-local
  hot_tidy:
    builtin: hot_tidy
    enabled: true
    schedule: "15 4 * * *"
```

Rules:

- `schedule:` is a string. Five whitespace-separated fields. No `@daily`
  shortcuts for v1 — keep parser trivial.
- A task without `schedule:` is skipped by the sync (operator runs it
  manually or installs cron by hand).
- A task with `enabled: false` is skipped, even if `schedule:` is present.
  Keeping the schedule line lets the operator flip the flag and re-sync
  without retyping the cadence.
- The timezone for the cron block is read from `gateway.yaml`'s
  `timezone:` field (same source the runner already uses for `tz_name`).
  Emitted as a `CRON_TZ=<zone>` line at the top of the block.

### CLI surface

Two subcommands added under the existing `jc heartbeat` binary:

```
jc heartbeat cron preview [--instance-dir <path>]
jc heartbeat cron sync    [--instance-dir <path>] [--dry-run]
```

- `preview` prints the cron lines that would be written. Read-only. Useful
  for `jc doctor` and for catching mistakes before `sync`.
- `sync` installs them into the calling user's crontab. Idempotent: a
  second run with no yaml change is a no-op. With `--dry-run`, prints the
  resulting crontab to stdout instead of installing it.

### Marker-wrapped block

The installed lines live inside a marker block keyed by the instance
basename, so multiple instances on one host coexist:

```
# === JC-HEARTBEAT BEGIN instance=rachel_zane ===
CRON_TZ=Asia/Dubai
30 3 * * * /usr/bin/jc heartbeat run dream_tick --instance-dir /home/lucamattei/rachel_zane >> /home/lucamattei/rachel_zane/state/logs/heartbeat-cron.log 2>&1
15 4 * * * /usr/bin/jc heartbeat run hot_tidy   --instance-dir /home/lucamattei/rachel_zane >> /home/lucamattei/rachel_zane/state/logs/heartbeat-cron.log 2>&1
# === JC-HEARTBEAT END instance=rachel_zane ===
```

On each `sync`:

1. Read the current crontab (`crontab -l`).
2. Strip any prior block matching this instance basename.
3. Build the new block from `tasks.yaml` (skipping disabled / missing
   schedule).
4. Append the block. Pipe the result through `crontab -`.

If `crontab -l` exits non-zero with "no crontab" (rc 1, empty output),
treat as empty.

Lines outside the marker block are preserved untouched — operators keep
their hand-written entries.

### `jc` path resolution

The cron line invokes the `jc` binary by absolute path. We resolve
`shutil.which("jc")` at sync time; if it isn't on PATH, abort with a clear
error ("install `jc` shims first"). No environment leakage between cron
and an interactive shell.

### Log path

Each line redirects stdout+stderr to
`<instance>/state/logs/heartbeat-cron.log`. The directory is created on
sync. The runner already writes its own per-task `state/run.log`; this
file just captures the wrapper-level surprises (`jc` not found, adapter
crashing on stderr).

## Anti-features

- **No `@reboot`, no `@daily` shortcuts.** Always 5 fields. Keeps parsing
  trivial and the preview readable.
- **No user-crontab editing for system cron.** v1 writes to `crontab -e`
  scope only. `/etc/cron.d/` support is a later concern.
- **No watch mode.** `sync` is idempotent; operators run it after editing
  `tasks.yaml`. A future heartbeat builtin can call it on a schedule, but
  not in v1.

## Acceptance criteria

1. `jc heartbeat cron preview` prints the expected block for a tasks.yaml
   with mixed enabled/disabled/no-schedule entries.
2. `jc heartbeat cron sync` installs the block. Re-running it produces a
   byte-identical crontab.
3. Editing `tasks.yaml` (flipping enabled, changing schedule, removing a
   task) and re-running `sync` replaces the prior block in place. Lines
   outside the markers are preserved.
4. `--dry-run` on `sync` prints the would-be crontab and does not touch
   the real one.
5. Disabled and schedule-less tasks are silently skipped (no warning
   noise — operator already sees them in `jc features list`).

## Tests

`tests/heartbeat/test_cron_sync.py` covers:

- `test_idempotent_resync` — running sync twice yields identical output.
- `test_replaces_prior_block` — changing schedule replaces marker block.
- `test_disabled_skipped` — `enabled: false` task absent from output.
- `test_missing_schedule_skipped` — task without `schedule:` absent.
- `test_dry_run_no_install` — `--dry-run` returns crontab string, does
  not invoke `crontab -`.
- `test_preserves_external_lines` — lines outside markers survive sync.
