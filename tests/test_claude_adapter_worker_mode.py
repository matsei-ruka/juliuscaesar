"""Smoke-test the WORKER MODE clause in lib/heartbeat/adapters/claude.sh.

Why this matters: workers run the claude adapter with cwd=instance_dir, so the
brain auto-loads the instance CLAUDE.md (routing rules, auto-memory pointing
"impl work → developer-01", etc.). With visibility into the workers DB, the
brain can pattern-match its own row as "another worker on the same topic" and
exit no-op. The recursion guard env var only suppresses sub-spawning. The
WORKER MODE clause appended to --append-system-prompt flips the role
explicitly. If this clause stops being emitted when JC_IN_WORKER=1, the
self-bail bug returns silently.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_ADAPTER = REPO_ROOT / "lib" / "heartbeat" / "adapters" / "claude.sh"


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """Build a HOME with a stub `claude` on PATH that prints its argv.

    claude.sh prepends $HOME/.local/bin to PATH, so the stub wins over any
    real claude install on the test runner.
    """
    bindir = tmp_path / ".local" / "bin"
    bindir.mkdir(parents=True)
    fake = bindir / "claude"
    fake.write_text(
        "#!/bin/bash\n"
        "for arg in \"$@\"; do printf '%s\\n--ARG-DELIM--\\n' \"$arg\"; done\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return tmp_path


def _run_adapter(home: Path, env_overrides: dict[str, str]) -> str:
    env = {k: v for k, v in os.environ.items() if k != "JC_IN_WORKER"}
    env["HOME"] = str(home)
    env.update(env_overrides)
    proc = subprocess.run(
        [str(CLAUDE_ADAPTER)],
        input=b"prompt\n",
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    return proc.stdout.decode()


def test_worker_mode_clause_present_when_jc_in_worker_set(fake_home: Path) -> None:
    out = _run_adapter(fake_home, {"JC_IN_WORKER": "1", "JC_WORKER_ID": "42"})
    assert "WORKER MODE:" in out, (
        "Expected WORKER MODE clause when JC_IN_WORKER=1; got:\n" + out
    )
    assert "Execute the brief inline." in out
    # Gateway contract still appended after the worker clause.
    assert "GATEWAY OUTPUT CONTRACT" in out


def test_worker_mode_clause_absent_when_jc_in_worker_unset(fake_home: Path) -> None:
    out = _run_adapter(fake_home, {})
    assert "WORKER MODE:" not in out, (
        "Did not expect WORKER MODE clause without JC_IN_WORKER; got:\n" + out
    )
    assert "GATEWAY OUTPUT CONTRACT" in out


def test_worker_mode_clause_absent_when_jc_in_worker_empty(fake_home: Path) -> None:
    out = _run_adapter(fake_home, {"JC_IN_WORKER": ""})
    assert "WORKER MODE:" not in out
