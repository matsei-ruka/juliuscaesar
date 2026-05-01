"""Tests for the minimal jc-setup behavior.

Covers:
- existing L1 files are preserved across re-runs
- --force overwrites L1 files
- existing .env secret values are preserved when prompts are empty
- the script no longer asks for mission / style / external-action policy
- re-running setup is idempotent
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_BIN = REPO_ROOT / "bin" / "jc-setup"


def _run_setup(target: Path, *args: str, stdin: str | None = None, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(SETUP_BIN), str(target), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _read_env(target: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (target / ".env").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, raw = line.partition("=")
        raw = raw.strip()
        if (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"')):
            raw = raw[1:-1]
        values[key] = raw
    return values


@pytest.fixture
def fresh_instance(tmp_path: Path) -> Path:
    """Fresh instance after a successful initial setup."""
    target = tmp_path / "instance"
    result = _run_setup(target, "--defaults", "--no-start", "--no-watchdog")
    assert result.returncode == 0, result.stderr
    return target


def test_setup_skips_existing_l1(fresh_instance: Path) -> None:
    custom = "# Custom identity content — operator hand-wrote this.\n"
    identity = fresh_instance / "memory" / "L1" / "IDENTITY.md"
    identity.write_text(custom, encoding="utf-8")

    result = _run_setup(fresh_instance, "--defaults", "--no-start", "--no-watchdog")
    assert result.returncode == 0, result.stderr
    assert identity.read_text(encoding="utf-8") == custom


def test_setup_force_overwrites_l1(fresh_instance: Path) -> None:
    custom = "# Custom identity content — operator hand-wrote this.\n"
    identity = fresh_instance / "memory" / "L1" / "IDENTITY.md"
    identity.write_text(custom, encoding="utf-8")

    result = _run_setup(fresh_instance, "--defaults", "--force", "--no-start", "--no-watchdog")
    assert result.returncode == 0, result.stderr

    after = identity.read_text(encoding="utf-8")
    assert after != custom
    assert "slug: IDENTITY" in after
    assert "is a JuliusCaesar assistant." in after


def test_setup_preserves_env_secrets(fresh_instance: Path) -> None:
    (fresh_instance / ".env").write_text(
        "# JuliusCaesar instance secrets. Do not commit.\n"
        "DASHSCOPE_API_KEY='ds-secret'\n"
        "TELEGRAM_BOT_TOKEN='preserved-abc'\n"
        "TELEGRAM_CHAT_ID='12345'\n"
        "SLACK_APP_TOKEN=''\n"
        "SLACK_BOT_TOKEN=''\n",
        encoding="utf-8",
    )

    result = _run_setup(fresh_instance, "--defaults", "--no-start", "--no-watchdog")
    assert result.returncode == 0, result.stderr

    env = _read_env(fresh_instance)
    assert env["DASHSCOPE_API_KEY"] == "ds-secret"
    assert env["TELEGRAM_BOT_TOKEN"] == "preserved-abc"
    assert env["TELEGRAM_CHAT_ID"] == "12345"


def test_setup_no_mission_prompt(tmp_path: Path) -> None:
    target = tmp_path / "instance"
    # Plenty of empty newlines to satisfy any remaining prompts; --no-start /
    # --no-watchdog skip the trailing yes/no questions.
    stdin = "\n" * 30
    result = _run_setup(target, "--no-start", "--no-watchdog", stdin=stdin)
    assert result.returncode == 0, result.stderr

    combined = (result.stderr + result.stdout).lower()
    assert "what should this assistant help with" not in combined
    assert "communication style" not in combined
    assert "external-action policy" not in combined


def test_setup_idempotent(fresh_instance: Path) -> None:
    l1 = fresh_instance / "memory" / "L1"
    env_path = fresh_instance / ".env"
    gateway = fresh_instance / "ops" / "gateway.yaml"

    snapshot = {
        "identity": (l1 / "IDENTITY.md").read_text(encoding="utf-8"),
        "user": (l1 / "USER.md").read_text(encoding="utf-8"),
        "rules": (l1 / "RULES.md").read_text(encoding="utf-8"),
        "hot": (l1 / "HOT.md").read_text(encoding="utf-8"),
        "env": env_path.read_text(encoding="utf-8"),
        "gateway": gateway.read_text(encoding="utf-8"),
    }

    result = _run_setup(fresh_instance, "--defaults", "--no-start", "--no-watchdog")
    assert result.returncode == 0, result.stderr

    assert (l1 / "IDENTITY.md").read_text(encoding="utf-8") == snapshot["identity"]
    assert (l1 / "USER.md").read_text(encoding="utf-8") == snapshot["user"]
    assert (l1 / "RULES.md").read_text(encoding="utf-8") == snapshot["rules"]
    assert (l1 / "HOT.md").read_text(encoding="utf-8") == snapshot["hot"]
    assert env_path.read_text(encoding="utf-8") == snapshot["env"]
    assert gateway.read_text(encoding="utf-8") == snapshot["gateway"]
