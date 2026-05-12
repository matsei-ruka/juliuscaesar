from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from release_updates import release_2026_05_12_01 as release_update  # noqa: E402


def test_migration_adds_disabled_tasks_and_config(tmp_path: Path) -> None:
    (tmp_path / ".jc").write_text("framework=juliuscaesar\n", encoding="utf-8")
    (tmp_path / "heartbeat").mkdir()
    (tmp_path / "heartbeat" / "tasks.yaml").write_text(
        "defaults: {}\ntasks: {}\n",
        encoding="utf-8",
    )

    assert release_update.main(["--instance-dir", str(tmp_path)]) == 0

    tasks = yaml.safe_load((tmp_path / "heartbeat" / "tasks.yaml").read_text())["tasks"]
    assert tasks["commitments_tick"] == {"builtin": "commitments_tick", "enabled": False}
    assert tasks["reengage_tick"] == {"builtin": "reengage_tick", "enabled": False}
    assert (tmp_path / "ops" / "reengage.yaml").exists()
    assert (tmp_path / "state" / "commitments" / "done").is_dir()
    assert (tmp_path / "state" / "commitments" / "failed").is_dir()
