from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))


def _load_migration_module():
    path = REPO_ROOT / "bin" / "jc-migrate-to-0.3-commitments"
    loader = importlib.machinery.SourceFileLoader("jc_migrate_commitments", str(path))
    spec = importlib.util.spec_from_loader("jc_migrate_commitments", loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_adds_disabled_tasks_and_config(tmp_path: Path) -> None:
    module = _load_migration_module()
    (tmp_path / ".jc").write_text("framework=juliuscaesar\n", encoding="utf-8")
    (tmp_path / "heartbeat").mkdir()
    (tmp_path / "heartbeat" / "tasks.yaml").write_text(
        "defaults: {}\ntasks: {}\n",
        encoding="utf-8",
    )

    assert module.main(["--instance-dir", str(tmp_path)]) == 0

    tasks = yaml.safe_load((tmp_path / "heartbeat" / "tasks.yaml").read_text())["tasks"]
    assert tasks["commitments_tick"] == {"builtin": "commitments_tick", "enabled": False}
    assert tasks["reengage_tick"] == {"builtin": "reengage_tick", "enabled": False}
    assert (tmp_path / "ops" / "reengage.yaml").exists()
    assert (tmp_path / "state" / "commitments" / "done").is_dir()
    assert (tmp_path / "state" / "commitments" / "failed").is_dir()
