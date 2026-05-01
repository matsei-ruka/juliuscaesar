"""CLI consistency smoke tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "lib") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        list(args),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


def test_bash_commands_have_help() -> None:
    for binary in ("jc-init", "jc-update", "jc-upgrade", "jc-doctor"):
        proc = _run(str(REPO_ROOT / "bin" / binary), "--help")
        assert proc.returncode == 0, proc.stderr
        assert "Usage:" in proc.stdout


def test_completion_command_outputs_shell_scripts() -> None:
    bash_proc = _run(str(REPO_ROOT / "bin" / "jc-completion"), "bash")
    assert bash_proc.returncode == 0, bash_proc.stderr
    assert "complete -F _jc_complete jc" in bash_proc.stdout

    zsh_proc = _run(str(REPO_ROOT / "bin" / "jc-completion"), "zsh")
    assert zsh_proc.returncode == 0, zsh_proc.stderr
    assert "#compdef jc" in zsh_proc.stdout


def test_workers_help_hides_internal_run_choice() -> None:
    proc = _run(sys.executable, str(REPO_ROOT / "bin" / "jc-workers"), "--help")
    assert proc.returncode == 0, proc.stderr
    assert "_run" not in proc.stdout


def test_router_lists_completion() -> None:
    proc = _run(str(REPO_ROOT / "bin" / "jc"), "--help")
    assert proc.returncode == 0, proc.stderr
    assert "completion" in proc.stdout
