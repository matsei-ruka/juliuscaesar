from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from heartbeat.runner import run_task  # noqa: E402


def test_dream_tick_heartbeat_builtin_runs_dry_when_disabled(tmp_path: Path) -> None:
    (tmp_path / ".jc").write_text("framework=juliuscaesar\n", encoding="utf-8")
    (tmp_path / "memory" / "L1").mkdir(parents=True)
    (tmp_path / "memory" / "L1" / "RULES.md").write_text("# RULES\n", encoding="utf-8")
    (tmp_path / "heartbeat").mkdir()
    (tmp_path / "heartbeat" / "tasks.yaml").write_text(
        """defaults: {}
tasks:
  dream_tick:
    builtin: dream_tick
    enabled: false
""",
        encoding="utf-8",
    )

    assert run_task(tmp_path, "dream_tick") == 0
    outputs = list((tmp_path / "heartbeat" / "state" / "outputs").glob("dream_tick-*.json"))
    assert outputs
    assert '"dry_run": true' in outputs[0].read_text(encoding="utf-8")
