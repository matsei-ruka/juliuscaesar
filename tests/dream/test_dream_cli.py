from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from dream.cli import main  # noqa: E402


def test_dream_list_empty(tmp_path: Path, capsys) -> None:
    (tmp_path / ".jc").write_text("framework=juliuscaesar\n", encoding="utf-8")
    assert main(["--instance-dir", str(tmp_path), "list"]) == 0
    assert "(no dreams)" in capsys.readouterr().out
