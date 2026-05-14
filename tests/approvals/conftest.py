"""Shared fixtures for approvals tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))


@pytest.fixture
def instance_dir(tmp_path: Path) -> Path:
    (tmp_path / ".jc").write_text("framework=juliuscaesar\n", encoding="utf-8")
    (tmp_path / "state").mkdir()
    (tmp_path / "memory").mkdir()
    (tmp_path / "ops").mkdir()
    return tmp_path
