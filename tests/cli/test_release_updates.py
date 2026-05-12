"""Release update hook coverage."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from release_updates import release_2026_05_02  # noqa: E402


def test_every_changelog_release_has_update_hook() -> None:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    releases = {
        match.group(1)
        for match in re.finditer(r"^## (20\d{2}\.\d{2}\.\d{2}(?:\.\d+)?)$", changelog, re.M)
    }
    assert releases
    missing = sorted(
        version
        for version in releases
        if not (REPO_ROOT / "updates" / "releases" / f"{version}.sh").exists()
    )
    assert missing == []


def test_release_hooks_are_executable() -> None:
    hooks = sorted((REPO_ROOT / "updates" / "releases").glob("*.sh"))
    assert hooks
    assert [hook.name for hook in hooks if not os.access(hook, os.X_OK)] == []


def test_gateway_release_hook_bootstraps_missing_config(tmp_path: Path) -> None:
    (tmp_path / ".jc").write_text("framework=juliuscaesar\n", encoding="utf-8")
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "watchdog.conf").write_text(
        "TELEGRAM_CHAT_ID=123\nRUNTIME_MODE=legacy-claude\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=t\n", encoding="utf-8")

    assert release_2026_05_02.main(["--instance-dir", str(tmp_path)]) == 0

    gateway = (tmp_path / "ops" / "gateway.yaml").read_text(encoding="utf-8")
    watchdog = (tmp_path / "ops" / "watchdog.conf").read_text(encoding="utf-8")
    assert "default_brain: claude" in gateway
    assert "RUNTIME_MODE=gateway" in watchdog
