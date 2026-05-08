"""Tests that jc-setup writes a `timezone:` line to gateway.yaml.

Covers docs/specs/timezone-config.md §Setup/upgrade UX:

- Running `jc-setup --defaults` with `JC_SETUP_ASSUME_BRAINS=claude` writes
  a `timezone:` line into ops/gateway.yaml whose value is either the host
  /etc/timezone contents or UTC.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_BIN = REPO_ROOT / "bin" / "jc-setup"


def _run_setup(target: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "JC_SETUP_ASSUME_BRAINS": "claude",
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "12345",
        }
    )
    return subprocess.run(
        [str(SETUP_BIN), str(target), "--defaults", "--no-start", "--no-watchdog", "--no-wait"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_setup_writes_timezone_to_gateway_yaml(tmp_path: Path) -> None:
    target = tmp_path / "instance"
    result = _run_setup(target)
    assert result.returncode == 0, result.stderr

    gateway_yaml = (target / "ops" / "gateway.yaml").read_text(encoding="utf-8")
    match = re.search(r"^timezone:\s*(\S+)", gateway_yaml, re.MULTILINE)
    assert match, f"timezone line missing from gateway.yaml:\n{gateway_yaml}"

    value = match.group(1)
    # Defaults path picks /etc/timezone when present, else UTC. Either is
    # acceptable for a host-agnostic test — just assert it's a non-empty
    # IANA-shaped token.
    assert value, "timezone value is empty"
    assert "/" in value or value == "UTC", f"unexpected timezone value: {value!r}"
