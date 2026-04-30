"""Smoke-test the JC_IN_WORKER env injection in jc-workers _run.

Why this matters: workers exec the brain adapter with cwd=instance_dir, so
claude/gemini load the instance CLAUDE.md → see "spawn workers for dev work"
→ recurse. JC_IN_WORKER=1 is the recursion guard. If the env var stops being
set, every worker re-spawns sub-workers on its own prompt.
"""

from __future__ import annotations

import os
import subprocess
import sqlite3
import sys
from pathlib import Path
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
JC_WORKERS = REPO_ROOT / "bin" / "jc-workers"


@pytest.fixture
def instance_dir(tmp_path: Path) -> Path:
    (tmp_path / "memory").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "gateway.yaml").write_text("channels: {}\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def echo_adapter(tmp_path: Path) -> Path:
    """Drop a temp adapter alongside the real ones so jc-workers finds it."""
    adapter_dir = REPO_ROOT / "lib" / "heartbeat" / "adapters"
    adapter = adapter_dir / "envtest.sh"
    adapter.write_text(
        '#!/bin/bash\n'
        'echo "JC_IN_WORKER=${JC_IN_WORKER:-unset}"\n'
        'echo "JC_WORKER_ID=${JC_WORKER_ID:-unset}"\n',
        encoding="utf-8",
    )
    adapter.chmod(0o755)
    yield adapter
    adapter.unlink(missing_ok=True)


def test_jc_in_worker_env_set_for_adapter(instance_dir: Path, echo_adapter: Path) -> None:
    spawn = subprocess.run(
        [
            sys.executable,
            str(JC_WORKERS),
            "--instance-dir",
            str(instance_dir),
            "spawn",
            "--topic",
            "env-smoke",
            "--brain",
            "envtest",
            "--prompt",
            "-",
        ],
        input=b"smoke prompt\n",
        capture_output=True,
        timeout=10,
    )
    assert spawn.returncode == 0, spawn.stderr.decode()

    db_path = instance_dir / "state" / "workers.db"
    deadline = 5.0
    import time

    waited = 0.0
    worker_id = None
    while waited < deadline:
        if db_path.exists():
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT id, status FROM workers ORDER BY id DESC LIMIT 1"
                ).fetchone()
            if row and row[1] == "done":
                worker_id = row[0]
                break
        time.sleep(0.2)
        waited += 0.2
    assert worker_id is not None, "worker did not finish in time"

    result = (
        instance_dir / "state" / "workers" / str(worker_id) / "result"
    ).read_text(encoding="utf-8")
    assert "JC_IN_WORKER=1" in result
    assert f"JC_WORKER_ID={worker_id}" in result
