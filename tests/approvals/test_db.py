"""DB schema + connection + idempotent init."""

from __future__ import annotations

from pathlib import Path

from approvals import db


def test_connect_creates_schema(instance_dir: Path) -> None:
    conn = db.connect(instance_dir)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='approvals'"
        ).fetchall()
        assert rows, "approvals table missing"
        cols = conn.execute("PRAGMA table_info(approvals)").fetchall()
        names = {r["name"] for r in cols}
        for required in (
            "approval_id",
            "kind",
            "title",
            "callback_token",
            "callback_kind",
            "status",
            "media_paths",
            "note",
        ):
            assert required in names
    finally:
        conn.close()


def test_connect_is_idempotent(instance_dir: Path) -> None:
    db.connect(instance_dir).close()
    db.connect(instance_dir).close()
    assert (instance_dir / "state" / "approvals.db").exists()


def test_init_for_instance(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    db.init_for_instance(tmp_path)
    assert (tmp_path / "state" / "approvals.db").exists()
