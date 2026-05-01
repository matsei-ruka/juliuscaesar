"""self_model_run — invoke the autonomous self-observation cycle.

Wraps `lib.self_model.runner.run_now(instance_dir)` as a heartbeat builtin so
the cycle can be cron-driven via `tasks.yaml`. Reads `ops/self_model.yaml`
for configuration; returns a summary dict.

Ships disabled. Operator enables by adding to `tasks.yaml`:

    self_model_run:
      builtin: self_model_run
      enabled: true
      # Recommended cadence: weekly (e.g. 0 9 * * 0). The cycle is idempotent
      # but each run that actually generates proposals calls the proposer LLM
      # — keep the cadence to once per signal-window or less.

Configuration: `<instance>/ops/self_model.yaml`. Default mode is `dry_run`,
so even if scheduled, no proposals are written until the operator promotes
the mode to `propose` or `apply`.
"""

from __future__ import annotations

from pathlib import Path


def run(instance_dir: Path, dry_run: bool = False) -> dict:
    """Execute one self-model cycle. Returns summary dict.

    `dry_run` here is the heartbeat-task-level dry-run (i.e., the operator
    asked `jc heartbeat run self_model_run --dry-run`). When True, we don't
    invoke the cycle at all and just report what would happen. The
    self-model has its OWN mode dimension (`dry_run | propose | apply`) in
    `ops/self_model.yaml` — those are independent.
    """
    if dry_run:
        return {
            "ok": True,
            "skipped": "heartbeat-level dry-run; self-model cycle not invoked",
        }

    # Imported lazily so this builtin loads cleanly even on instances that
    # haven't installed the self_model package yet.
    from self_model.runner import run_now  # type: ignore

    rc = run_now(instance_dir)
    return {
        "ok": rc == 0,
        "rc": rc,
        "log": str(instance_dir / "heartbeat" / "state" / "self_model.log"),
    }
