# Spec: Fix `jc-user-model install` crontab clobbering

**Status:** Draft — pending review
**Date:** 2026-05-14
**Branch:** `spec/jc-user-model-install-fix`
**Scope:** Single-file bug fix in `lib/user_model/cli.py`. Same fix pattern applied to `cmd_uninstall`. Optional follow-up: parity install subcommands for `jc-dream` and `jc-self-model`.

---

## 1. Problem

`jc-user-model install --instance-dir <path>` strips **every** `jc-user-model` cron line from the operator's crontab (across all instances on the host), then appends a single line for the current instance. Running `install` on the second instance silently removes the first instance's entry.

Observed 2026-05-14 on `lucamattei@192.168.3.246`: installed user-model crons for rachel/marco/harold/anika sequentially; only anika survived. Same defect exists in `cmd_uninstall` — it removes user-model crons for **all** instances when called for one.

Root cause is at `lib/user_model/cli.py:166-180`:

```python
proc = subprocess.run(
    ["bash", "-c",
     f"(crontab -l 2>/dev/null || true) | grep -v '{binary}' | crontab - && "
     f"crontab -l | grep -q '{binary}' || "
     f"(crontab -l 2>/dev/null; echo '{cron_line}') | crontab -"],
    ...
)
```

The `grep -v '{binary}'` step (binary = `"jc-user-model"`) matches every line containing the substring `jc-user-model` — not just the one for `--instance-dir <path>`.

`cmd_uninstall` (lines 183-196) has the symmetric defect.

---

## 2. Why this exists / blast radius

JC fleets run multiple instances per host (Luca's local box: rachel + marco + harold + anika; remote box: florian + sofia + adrian + sarah + victoria). Operators install user-model on each instance independently. The current behaviour silently breaks the most common multi-instance workflow.

Beyond user-model, the same shell pattern is the obvious template for any future `jc-<x> install` cron helper, so fixing this once stops the same bug from being copy-pasted.

---

## 3. Fix

Match by the **per-instance marker comment** that the install line already writes:

```
0 3 * * * /home/lucamattei/.local/bin/jc-user-model run-now --instance-dir <path>  # jc-user-model for <path>
```

The trailing comment `# jc-user-model for <path>` is unique per instance. Strip-and-append on that exact substring, not on the binary name.

### Replacement: `cmd_install`

```python
def cmd_install(instance_dir: Path, cadence: str) -> int:
    """Install (or replace) the user-model cron task for this instance."""
    import subprocess
    binary = shutil.which("jc-user-model") or "jc-user-model"
    instance_dir = instance_dir.resolve()
    marker = f"# jc-user-model for {instance_dir}"
    cron_line = (
        f"{cadence} {binary} run-now --instance-dir {instance_dir}  {marker}"
    )
    script = (
        f"(crontab -l 2>/dev/null || true) "
        f"| grep -vF {shlex.quote(marker)} "
        f"| (cat; echo {shlex.quote(cron_line)}) "
        f"| crontab -"
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    if proc.returncode == 0:
        print("Cron task installed")
        return 0
    print(f"Failed to install cron: {proc.stderr}", file=sys.stderr)
    return 1
```

Notes:

- `shlex.quote()` on `marker` and `cron_line` prevents injection / quoting bugs (e.g. if `instance_dir` contains a space).
- `grep -vF` is fixed-string match — no regex surprises with `.` or `+` in paths.
- `instance_dir.resolve()` normalises the path so a `~` or relative input still matches at idempotent rerun.
- Resolving `binary` via `shutil.which()` keeps the cron line absolute (cron's PATH is minimal).
- Idempotent: rerunning with the same `instance_dir` replaces in place.

### Replacement: `cmd_uninstall`

```python
def cmd_uninstall(instance_dir: Path) -> int:
    """Remove the user-model cron task for this instance only."""
    import subprocess
    instance_dir = instance_dir.resolve()
    marker = f"# jc-user-model for {instance_dir}"
    script = (
        f"(crontab -l 2>/dev/null || true) "
        f"| grep -vF {shlex.quote(marker)} "
        f"| crontab -"
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    if proc.returncode == 0:
        print("Cron task removed")
        return 0
    print(f"Failed to uninstall cron: {proc.stderr}", file=sys.stderr)
    return 1
```

### Imports

Add at top of `lib/user_model/cli.py`:

```python
import shlex
import shutil
```

---

## 4. Test plan

`tests/user_model/test_cli_install.py` (new file). Patch `subprocess.run` to:

- Capture the bash script string.
- Return a configurable stdout (the prior crontab) and rc=0.

### Tests

1. **install_appends_when_absent** — empty crontab, `install /home/foo/rachel` produces a script that ends in a single cron line referencing `/home/foo/rachel`.
2. **install_preserves_other_instances** — prior crontab has user-model lines for `rachel` and `marco`. `install /home/foo/harold` keeps both, adds harold.
3. **install_replaces_same_instance** — prior crontab has user-model for `rachel`. `install /home/foo/rachel --cadence "30 3 * * *"` keeps only the new line, with the new cadence.
4. **install_quotes_spaces** — `instance_dir = Path("/tmp/with space/inst")` produces a properly-quoted script that survives `bash -c`.
5. **uninstall_removes_only_this_instance** — prior crontab has rachel + marco + harold. `uninstall /home/foo/marco` leaves rachel + harold.
6. **uninstall_noop_when_absent** — prior crontab has only rachel. `uninstall /home/foo/marco` is a no-op (rc=0, rachel survives).

No integration test against the real crontab. The unit tests assert the script content; `bash -c` semantics are tested by the host.

---

## 5. Migration / backfill

None. The fix is purely behavioural inside the `install` command. Operators who lost cron entries to the previous bug must rerun `install` for each instance (this PR's fixed version is correct from the first call).

The PR description should call out the bug + list how to re-install:

```
for inst in rachel marco harold anika; do
  jc-user-model --instance-dir /home/lucamattei/$inst install
done
```

---

## 6. Out of scope (intentional)

- **Bespoke `install` subcommands for `jc-dream` and `jc-self-model`** — both lack the install/uninstall subcommand entirely; operators add their cron lines by hand. A follow-up PR can add parity install commands using the corrected pattern from this spec. Tracking note added to spec but not implemented here.
- **`jc heartbeat` cron management** — outside this fix's surface.

---

## 7. Risk

Low. `cmd_install` / `cmd_uninstall` are pure cron-line manipulation; no DB, no state writes, no concurrent access. Tested under mocked `subprocess.run`. The worst regression is a non-zero exit code — operator notices immediately.

---

## 8. Open questions

1. Should the fix also normalise existing user-model cron lines to the new marker format on first install (back-compat sweep), or leave legacy lines alone (operator rerun)? Recommend: leave them alone — explicit rerun.
2. Should `cmd_install` print the diff (lines added/removed) so the operator sees what changed? Cheap to add, slight scope creep. Defer.
3. Follow-up scope: add identical `install` / `uninstall` to `jc-dream` and `jc-self-model` in a separate PR using this pattern. Open.
