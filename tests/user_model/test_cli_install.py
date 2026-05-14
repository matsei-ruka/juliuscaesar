"""Tests for jc-user-model install/uninstall crontab handling."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from unittest.mock import patch

from lib.user_model.cli import cmd_install, cmd_uninstall


class _FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _run_script_with_prior(script: str, prior_crontab: str) -> str:
    """Execute the install/uninstall bash script against a fake prior crontab.

    Replaces `crontab -l` with `printf %s <prior>` and `crontab -` with `cat`
    so we can observe the final crontab content that the real `crontab -`
    would have received on stdin.
    """
    safe_prior = shlex.quote(prior_crontab)
    rewritten = (
        script
        .replace("crontab -l 2>/dev/null || true", f"printf %s {safe_prior}")
        .replace("crontab -l", f"printf %s {safe_prior}")
        .replace("crontab -", "cat")
    )
    proc = subprocess.run(
        ["bash", "-c", rewritten], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _capture_script() -> tuple[list[str], _FakeCompleted]:
    """Returns (captured, fake_completed) — append `captured.append(cmd[2])`."""
    captured: list[str] = []
    fake = _FakeCompleted(returncode=0)

    def _fake_run(cmd, capture_output=True, text=True):
        assert cmd[0] == "bash" and cmd[1] == "-c"
        captured.append(cmd[2])
        return fake

    return captured, _fake_run


def test_install_appends_when_absent():
    captured, fake_run = _capture_script()
    with patch("subprocess.run", side_effect=fake_run):
        rc = cmd_install(Path("/home/foo/rachel"), "0 3 * * *")
    assert rc == 0
    assert len(captured) == 1
    script = captured[0]

    final = _run_script_with_prior(script, prior_crontab="")
    lines = [line for line in final.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "/home/foo/rachel" in lines[0]
    assert "# jc-user-model for /home/foo/rachel" in lines[0]


def test_install_preserves_other_instances():
    prior = (
        "0 3 * * * /usr/local/bin/jc-user-model run-now --instance-dir "
        "/home/foo/rachel  # jc-user-model for /home/foo/rachel\n"
        "0 3 * * * /usr/local/bin/jc-user-model run-now --instance-dir "
        "/home/foo/marco  # jc-user-model for /home/foo/marco\n"
    )
    captured, fake_run = _capture_script()
    with patch("subprocess.run", side_effect=fake_run):
        rc = cmd_install(Path("/home/foo/harold"), "0 3 * * *")
    assert rc == 0

    final = _run_script_with_prior(captured[0], prior_crontab=prior)
    assert "# jc-user-model for /home/foo/rachel" in final
    assert "# jc-user-model for /home/foo/marco" in final
    assert "# jc-user-model for /home/foo/harold" in final
    assert final.count("# jc-user-model for /home/foo/harold") == 1


def test_install_replaces_same_instance():
    prior = (
        "0 3 * * * /usr/local/bin/jc-user-model run-now --instance-dir "
        "/home/foo/rachel  # jc-user-model for /home/foo/rachel\n"
    )
    captured, fake_run = _capture_script()
    with patch("subprocess.run", side_effect=fake_run):
        rc = cmd_install(Path("/home/foo/rachel"), "30 3 * * *")
    assert rc == 0

    final = _run_script_with_prior(captured[0], prior_crontab=prior)
    rachel_lines = [
        line for line in final.splitlines()
        if "# jc-user-model for /home/foo/rachel" in line
    ]
    assert len(rachel_lines) == 1
    assert rachel_lines[0].startswith("30 3 * * *")


def test_install_quotes_spaces(tmp_path):
    weird_dir = tmp_path / "with space" / "inst"
    weird_dir.mkdir(parents=True)
    captured, fake_run = _capture_script()
    with patch("subprocess.run", side_effect=fake_run):
        rc = cmd_install(weird_dir, "0 3 * * *")
    assert rc == 0
    script = captured[0]
    final = _run_script_with_prior(script, prior_crontab="")
    lines = [line for line in final.splitlines() if line.strip()]
    assert len(lines) == 1
    assert f"# jc-user-model for {weird_dir}" in lines[0]
    assert str(weird_dir) in lines[0]


def test_uninstall_removes_only_this_instance():
    prior = (
        "0 3 * * * /usr/local/bin/jc-user-model run-now --instance-dir "
        "/home/foo/rachel  # jc-user-model for /home/foo/rachel\n"
        "0 3 * * * /usr/local/bin/jc-user-model run-now --instance-dir "
        "/home/foo/marco  # jc-user-model for /home/foo/marco\n"
        "0 3 * * * /usr/local/bin/jc-user-model run-now --instance-dir "
        "/home/foo/harold  # jc-user-model for /home/foo/harold\n"
    )
    captured, fake_run = _capture_script()
    with patch("subprocess.run", side_effect=fake_run):
        rc = cmd_uninstall(Path("/home/foo/marco"))
    assert rc == 0

    final = _run_script_with_prior(captured[0], prior_crontab=prior)
    assert "# jc-user-model for /home/foo/rachel" in final
    assert "# jc-user-model for /home/foo/harold" in final
    assert "# jc-user-model for /home/foo/marco" not in final


def test_uninstall_noop_when_absent():
    prior = (
        "0 3 * * * /usr/local/bin/jc-user-model run-now --instance-dir "
        "/home/foo/rachel  # jc-user-model for /home/foo/rachel\n"
    )
    captured, fake_run = _capture_script()
    with patch("subprocess.run", side_effect=fake_run):
        rc = cmd_uninstall(Path("/home/foo/marco"))
    assert rc == 0

    final = _run_script_with_prior(captured[0], prior_crontab=prior)
    assert "# jc-user-model for /home/foo/rachel" in final
    assert "# jc-user-model for /home/foo/marco" not in final
