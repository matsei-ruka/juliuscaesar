"""Smoke-test the WORKER MODE override in lib/heartbeat/adapters/claude.sh.

Why this matters: workers exec the claude adapter with cwd=instance_dir, so
claude auto-loads CLAUDE.md and applies live-session routing logic. Without
an explicit override, the worker queries the workers DB, sees itself listed,
classifies as a duplicate and exits with no work done. JC_IN_WORKER=1 must
flip claude into executor mode via --append-system-prompt.

Strategy: shim the `claude` binary on PATH with a script that prints its
argv. Run the adapter with and without JC_IN_WORKER and assert the
WORKER MODE prompt is/isn't appended.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER = REPO_ROOT / "lib" / "heartbeat" / "adapters" / "claude.sh"


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """Build a HOME with a `claude` shim under .local/bin/.

    The adapter prepends `$HOME/.local/bin` to PATH at the top of the
    script, so dropping a shim there guarantees it wins over any real
    claude install on the runner.
    """
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "claude"
    shim.write_text(
        '#!/bin/bash\n'
        'for a in "$@"; do echo "$a"; done\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return tmp_path


def _run_adapter(home: Path, env_overrides: dict[str, str]) -> str:
    # Strip any session-resume vars from the parent env so the adapter
    # doesn't try to resume a real claude session and short-circuit the
    # shim.
    parent_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("JC_RESUME_SESSION", "WORKER_RESUME_SESSION", "JC_IN_WORKER")
    }
    env = {
        **parent_env,
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",  # adapter prepends $HOME/.local/bin
        **env_overrides,
    }
    proc = subprocess.run(
        ["bash", str(ADAPTER)],
        input=b"",
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    return proc.stdout.decode()


def test_worker_mode_prompt_present_when_in_worker(fake_home: Path) -> None:
    out = _run_adapter(fake_home, {"JC_IN_WORKER": "1"})
    assert "WORKER MODE" in out, "expected WORKER MODE prompt when JC_IN_WORKER=1"
    assert "GATEWAY OUTPUT CONTRACT" in out, "gateway contract must still be present"


def test_worker_mode_prompt_absent_outside_worker(fake_home: Path) -> None:
    out = _run_adapter(fake_home, {})
    assert "WORKER MODE" not in out, "WORKER MODE must not leak into non-worker runs"
    assert "GATEWAY OUTPUT CONTRACT" in out
