# Watchdog self-install + crontab guard

## Problem

`bin/jc-watchdog install` already writes per-instance cron entries
tagged with `# jc-watchdog for <instance>`. But the cron block is the
single point of liveness for everything else — if it's missing, the
daemon never restarts, and no other check notices.

On 2026-05-29, Ethan Zhang's gateway stopped at 14:00:41Z. By the time
the outage surfaced (2026-06-01), `crontab -u jc -l` was empty: the
watchdog block was gone. The most likely cause is the very operation
that stopped the gateway — a manual `crontab -e` that overwrote the
file, or an install script that piped a fresh crontab. Either way,
`jc doctor` ran clean throughout (it only checks pidfiles, not the
cron line that would have restarted the dead daemon).

Two things must change:

1. The install pattern needs to match the marker-block convention used
   by `heartbeat cron sync` (PR #74) so the same idempotent
   replace/strip primitives apply. Today's `# jc-watchdog for <dir>`
   trailing tag works but is fragile under multi-edit crontabs.
2. `jc doctor` must fail when the block is missing, not info-log it.

## Solution

### New module: `lib/watchdog/install.py`

Pure functions, no side effects on import. Public API:

```python
BEGIN_MARKER = "# === JC-WATCHDOG BEGIN instance="
END_MARKER   = "# === JC-WATCHDOG END instance="

def build_block(instance_dir: Path, *, jc_binary: str | None = None,
                tick_interval_minutes: int = 2) -> str
def read_current_crontab() -> str
def strip_block(crontab_text: str, instance_basename: str) -> str
def compose_crontab(prior: str, block: str, basename: str) -> str
def install(instance_dir: Path, *, dry_run: bool = False,
            jc_binary: str | None = None,
            crontab_reader=None, crontab_writer=None) -> dict
def verify(instance_dir: Path, *, tick_interval_minutes: int = 2,
           crontab_reader=None) -> Finding
```

The marker pattern mirrors `lib/heartbeat/cron_sync.py` so the two
blocks coexist cleanly. The verify finding reuses the existing
`Finding` dataclass shape (level + message).

#### Block content

```
# === JC-WATCHDOG BEGIN instance=<basename> ===
*/2 * * * * /usr/local/bin/jc watchdog tick --instance-dir <instance-dir>
@reboot     /usr/local/bin/jc watchdog tick --instance-dir <instance-dir>
# === JC-WATCHDOG END instance=<basename> ===
```

The `jc_binary` argument defaults to `shutil.which("jc")` — same
pattern as `cron_sync._resolve_jc_binary()`. `tick_interval_minutes`
is a parameter so verify can match what install wrote.

#### Verify semantics

`verify()` returns:

- `Finding("ok", "watchdog cron block present (instance=<name>)")` if
  the block exists and contains a tick line matching
  `*/N * * * *` for the requested interval **and** a `@reboot` line.
- `Finding("fail", "watchdog cron block missing")` otherwise.

It does **not** repair the block — that's `install`'s job. Verify is
read-only.

### CLI surface

Two new subcommands wired into `bin/jc-watchdog` alongside the existing
`install` (which is preserved for backward compat — see migration
below):

```
jc watchdog install [--instance-dir <dir>] [--dry-run]
jc watchdog verify  [--instance-dir <dir>]
```

`install` now writes the marker block via the Python module instead of
the inline bash. The `# jc-watchdog for <dir>` legacy tag lines (if
present from a previous install) are stripped during compose so the
upgrade is a clean cutover.

`verify` exits 0 on `ok`, 1 on `fail`. Prints the finding message.

### Doctor wiring

In `bin/jc-doctor`'s `Runtime (watchdog)` section, replace the
current `crontab -l | grep -qF "$TAG"` heuristic with a call to
`watchdog.install.verify(...)`. The OK/FAIL prefix flows through the
existing parser; FAIL increments the counter → non-zero exit.

### Acceptance

1. Fresh `jc watchdog install` writes a marker block; second run is a
   no-op (idempotent).
2. Re-running install after editing the block by hand replaces the
   block in place; surrounding crontab lines are preserved.
3. `jc watchdog verify` exits 0 when the block is present, 1 when
   absent or when the tick line cadence doesn't match.
4. `jc doctor` exits non-zero when the watchdog block is missing.
5. Legacy `# jc-watchdog for <dir>` tagged lines from prior installs
   are removed when the new install runs.
6. Unit tests cover all of the above without invoking real `crontab`
   (the install/verify helpers accept reader/writer test seams).
