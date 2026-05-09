"""STYLE.md controls framework caveman preamble injection."""

from __future__ import annotations

from pathlib import Path

from gateway import context


def _instance(tmp_path: Path, style: str | None = None) -> Path:
    instance = tmp_path / "instance"
    l1 = instance / "memory" / "L1"
    l1.mkdir(parents=True)
    (l1 / "IDENTITY.md").write_text("identity", encoding="utf-8")
    if style is not None:
        (l1 / "STYLE.md").write_text(style, encoding="utf-8")
    context.clear_cache()
    return instance


def test_caveman_disabled_omits_token_efficiency_block(tmp_path: Path) -> None:
    instance = _instance(tmp_path, "# Voice anchor\n\n> voice\n\n## Caveman\n\ncaveman: disabled\n")

    text = context.render_preamble(instance)

    assert "Token efficiency (caveman mode)" not in text
    assert "/caveman" not in text


def test_caveman_enabled_and_missing_style_keep_existing_default(tmp_path: Path) -> None:
    enabled = _instance(tmp_path / "enabled", "# Voice anchor\n\n> voice\n\ncaveman: enabled\n")
    missing = _instance(tmp_path / "missing")

    assert "Token efficiency (caveman mode)" in context.render_preamble(enabled)
    assert "Token efficiency (caveman mode)" in context.render_preamble(missing)
