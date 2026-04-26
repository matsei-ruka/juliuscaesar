"""Legacy `claude-session` child type.

Wraps the existing `watchdog.sh` script's `main()` path (screen + claude + bun
telegram plugin asymmetric-state cleanup). The bash script is still the source
of truth for that flow; this module just shells out to it with the env vars
the script expects, so the supervisor can supervise legacy instances during
the v1→v2 transition without forking the bash logic.

This child type is **deprecated** — slated for removal in 0.5.0 along with
the screen + bun-plugin code path it wraps.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .child import ChildSpec


WATCHDOG_SH = Path(__file__).resolve().parent / "watchdog.sh"


def restart(spec: ChildSpec, instance_dir: Path, log_file: Path) -> tuple[int, str]:
    """Re-run the bash watchdog tick in legacy-claude mode.

    The script's `main()` performs the kill-stale → kill-orphan-plugin →
    screen-respawn dance. We invoke it once per restart attempt; the script
    exits 0 on success and non-zero on failure, which the supervisor counts
    against the restart budget.
    """
    env = os.environ.copy()
    env["RUNTIME_MODE"] = "legacy-claude"
    if spec.screen_name:
        env["SCREEN_NAME"] = spec.screen_name
    if spec.session_id:
        env["SESSION_ID"] = spec.session_id
    extra_args = spec.extra.get("claude_args_extra")
    if extra_args:
        env["CLAUDE_ARGS_EXTRA"] = str(extra_args)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab") as handle:
        proc = subprocess.run(
            ["bash", str(WATCHDOG_SH), str(instance_dir)],
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
        )
    return proc.returncode, str(WATCHDOG_SH)
