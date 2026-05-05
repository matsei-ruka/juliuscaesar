"""Tests for the terminal persona interview prompter."""

from __future__ import annotations

import builtins
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from persona_interview.cli import TerminalPrompter  # noqa: E402


def _feed(monkeypatch, lines: list[str]) -> None:
    remaining = iter(lines)

    def fake_input(_prompt: str = "") -> str:
        return next(remaining)

    monkeypatch.setattr(builtins, "input", fake_input)


def test_longtext_preserves_internal_blank_lines(monkeypatch):
    _feed(monkeypatch, ["Personal Details", "", "Full name: Florian Berger", "EOF"])

    text = TerminalPrompter(color=False)._ask_longtext()

    assert text == "Personal Details\n\nFull name: Florian Berger"


def test_longtext_first_line_eof_skips(monkeypatch):
    _feed(monkeypatch, ["EOF"])

    text = TerminalPrompter(color=False)._ask_longtext()

    assert text is None


def test_longtext_empty_body_before_eof_skips(monkeypatch):
    _feed(monkeypatch, ["", "", "EOF"])

    text = TerminalPrompter(color=False)._ask_longtext()

    assert text is None
