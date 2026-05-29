"""Tests for jc-skills."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_BIN = REPO_ROOT / "bin" / "jc-init"
SKILLS_BIN = REPO_ROOT / "bin" / "jc-skills"
ROUTER_BIN = REPO_ROOT / "bin" / "jc"


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "lib") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        list(args),
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _init_instance(tmp_path: Path) -> Path:
    instance = tmp_path / "instance"
    result = _run(str(INIT_BIN), str(instance))
    assert result.returncode == 0, result.stderr
    return instance


def test_status_json_reports_preshipped_inactive(tmp_path: Path) -> None:
    instance = _init_instance(tmp_path)

    result = _run(str(SKILLS_BIN), "--instance-dir", str(instance), "status", "--json")
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout)
    rows = {row["name"]: row for row in payload["skills"]}
    assert set(rows) >= {"brave", "tavily", "firecrawl", "browser-use", "taskgraph"}
    assert rows["brave"]["installed"] is True
    assert rows["brave"]["status"] == "inactive"
    assert rows["brave"]["missing_env"] == ["BRAVE_API_KEY"]
    assert rows["taskgraph"]["status"] == "inactive"
    assert rows["taskgraph"]["missing_env"] == ["COMPANY_ENDPOINT", "COMPANY_API_KEY"]


def test_configure_sets_env_and_marks_skill_active(tmp_path: Path) -> None:
    instance = _init_instance(tmp_path)

    result = _run(
        str(SKILLS_BIN),
        "--instance-dir",
        str(instance),
        "configure",
        "brave",
        "--set",
        "BRAVE_API_KEY=brave-secret",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert "brave-secret" not in result.stdout

    env_text = (instance / ".env").read_text(encoding="utf-8")
    assert "BRAVE_API_KEY=brave-secret" in env_text

    status = _run(str(SKILLS_BIN), "--instance-dir", str(instance), "status", "--json")
    rows = {row["name"]: row for row in json.loads(status.stdout)["skills"]}
    assert rows["brave"]["status"] == "active"
    assert rows["brave"]["missing_env"] == []


def test_test_missing_credential_records_failure_without_network(tmp_path: Path) -> None:
    instance = _init_instance(tmp_path)

    result = _run(str(SKILLS_BIN), "--instance-dir", str(instance), "test", "tavily", "--json")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["results"][0]["ok"] is False
    assert payload["results"][0]["message"] == "missing TAVILY_API_KEY"

    state = json.loads((instance / "state" / "skills" / "status.json").read_text(encoding="utf-8"))
    assert state["skills"]["tavily"]["message"] == "missing TAVILY_API_KEY"


def test_sync_copies_preshipped_skills_into_older_instance(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    (instance / "skills").mkdir(parents=True)
    (instance / ".jc").write_text("framework=juliuscaesar\n", encoding="utf-8")
    (instance / ".env").write_text("# test\n", encoding="utf-8")

    result = _run(str(SKILLS_BIN), "--instance-dir", str(instance), "sync", "--json")
    assert result.returncode == 0, result.stderr

    assert (instance / "skills" / "Index.md").exists()
    assert (instance / "skills" / "brave" / "SKILL.md").exists()
    assert (instance / "skills" / "browser-use" / "SKILL.md").exists()
    assert (instance / "skills" / "taskgraph" / "SKILL.md").exists()


def test_router_lists_and_dispatches_skills(tmp_path: Path) -> None:
    instance = _init_instance(tmp_path)

    help_proc = _run(str(ROUTER_BIN), "--help")
    assert help_proc.returncode == 0, help_proc.stderr
    assert "skills" in help_proc.stdout

    path_env = os.environ.copy()
    path_env["PATH"] = str(REPO_ROOT / "bin") + os.pathsep + path_env.get("PATH", "")
    path_env["PYTHONPATH"] = str(REPO_ROOT / "lib") + os.pathsep + path_env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [str(ROUTER_BIN), "--instance-dir", str(instance), "skills", "status", "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=path_env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["skills"][0]["name"] == "brave"
