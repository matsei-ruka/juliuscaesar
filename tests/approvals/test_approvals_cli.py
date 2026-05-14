"""CLI surface tests — list, show, approve gate for SENSITIVE."""

from __future__ import annotations

from pathlib import Path

from approvals.cli import main as cli_main
from approvals.service import raise_


def test_list_pending_empty(instance_dir: Path, capsys) -> None:
    rc = cli_main(["--instance-dir", str(instance_dir), "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(none)" in out


def test_list_pending_shows_row(instance_dir: Path, capsys) -> None:
    rec = raise_(
        instance_dir,
        kind="action",
        title="hello",
        payload={"description": "x"},
        producer="test",
        notify_telegram=False,
    )
    rc = cli_main(["--instance-dir", str(instance_dir), "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert rec.short_id in out
    assert "hello" in out


def test_cli_approve_blocks_sensitive(instance_dir: Path, capsys) -> None:
    rec = raise_(
        instance_dir,
        kind="self_model_diff",
        title="bad",
        payload={
            "proposal_id": "p1",
            "target_file": "memory/L1/RULES.md",
            "target_section": "## §3",
            "diff": "x",
            "risk_class": "SENSITIVE",
        },
        producer="self_model",
        notify_telegram=False,
    )
    rc = cli_main(
        ["--instance-dir", str(instance_dir), "approve", rec.approval_id]
    )
    assert rc == 9
    err = capsys.readouterr().err
    assert "SENSITIVE" in err


def test_cli_approve_action(instance_dir: Path, capsys) -> None:
    rec = raise_(
        instance_dir,
        kind="action",
        title="ok",
        payload={"description": "x"},
        producer="test",
        notify_telegram=False,
    )
    rc = cli_main(
        ["--instance-dir", str(instance_dir), "approve", rec.approval_id]
    )
    assert rc == 0


def test_cli_show_by_short_id(instance_dir: Path, capsys) -> None:
    rec = raise_(
        instance_dir,
        kind="action",
        title="hello",
        payload={"description": "x"},
        producer="test",
        notify_telegram=False,
    )
    rc = cli_main(["--instance-dir", str(instance_dir), "show", rec.short_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert rec.approval_id in out
