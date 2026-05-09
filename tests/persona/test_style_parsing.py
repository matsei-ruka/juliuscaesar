"""STYLE.md parsing helpers."""

from __future__ import annotations

from pathlib import Path

from gateway import context


def _write_style(instance: Path, body: str) -> None:
    l1 = instance / "memory" / "L1"
    l1.mkdir(parents=True, exist_ok=True)
    (l1 / "STYLE.md").write_text(body, encoding="utf-8")
    context.clear_cache()


def test_voice_anchor_extracts_last_blockquote(tmp_path: Path) -> None:
    _write_style(
        tmp_path,
        """
# Voice anchor

Longer explanation here.
> First draft.
> Stay sharp, warm, and unmistakably yourself.

## Caveman

caveman: disabled
""".lstrip(),
    )

    assert context.render_voice_anchor(tmp_path) == "Stay sharp, warm, and unmistakably yourself."


def test_voice_anchor_missing_or_too_long_degrades_to_empty(tmp_path: Path) -> None:
    _write_style(tmp_path, "# Voice anchor\n\nNo blockquote here.\n")
    assert context.render_voice_anchor(tmp_path) == ""

    _write_style(tmp_path, "# Voice anchor\n\n> " + ("x" * 301) + "\n")
    assert context.render_voice_anchor(tmp_path) == ""


def test_caveman_flag_defaults_on_and_honors_disabled(tmp_path: Path) -> None:
    assert context.caveman_enabled(tmp_path) is True

    _write_style(tmp_path, "# Voice anchor\n\n> voice\n\n## Caveman\n\ncaveman: disabled\n")
    assert context.caveman_enabled(tmp_path) is False

    _write_style(tmp_path, "# Voice anchor\n\n> voice\n\n## Caveman\n\ncaveman: enabled\n")
    assert context.caveman_enabled(tmp_path) is True
