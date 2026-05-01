"""Tests for lib/persona_interview/compose.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from persona_interview.compose import compose, visible_prompts  # noqa: E402
from persona_interview.questions import (  # noqa: E402
    Composition,
    Dependency,
    Prompt,
    Slot,
    Validation,
)


def _slot(*, kind="text", prompts, composition=None) -> Slot:
    return Slot(
        slot_id="x.y",
        target_file="memory/L1/RULES.md",
        target_section="## X",
        placeholder="{{slot:x.y}}",
        applicability=("always",),
        kind=kind,
        status="exemplar",
        prompts=tuple(prompts),
        composition=composition,
    )


def _p(id_, kind="text", depends_on=None, choices=()) -> Prompt:
    return Prompt(
        id=id_, text=f"Q {id_}", kind=kind,
        choices=choices, examples=(),
        validation=Validation(),
        depends_on=depends_on,
    )


# ---------------------------------------------------------------------------
# Single-prompt slots
# ---------------------------------------------------------------------------

def test_compose_text():
    s = _slot(kind="text", prompts=[_p("q")])
    assert compose(s, {"q": "Hello world"}) == "Hello world\n"


def test_compose_text_empty():
    s = _slot(kind="text", prompts=[_p("q")])
    assert compose(s, {"q": ""}) == ""


def test_compose_list_bullets_each_line():
    s = _slot(kind="list", prompts=[_p("q", kind="list")])
    out = compose(s, {"q": "alpha\nbeta\ngamma"})
    assert out == "- alpha\n- beta\n- gamma\n"


def test_compose_list_keeps_existing_bullets():
    s = _slot(kind="list", prompts=[_p("q", kind="list")])
    out = compose(s, {"q": "- alpha\n* beta\ngamma"})
    assert "- alpha" in out
    assert "* beta" in out
    assert "- gamma" in out


def test_compose_choice():
    s = _slot(kind="choice", prompts=[_p("q", kind="choice", choices=("yes", "no"))])
    assert compose(s, {"q": "yes"}) == "yes\n"


# ---------------------------------------------------------------------------
# Structured slots
# ---------------------------------------------------------------------------

def test_compose_structured_template_substitutes():
    s = _slot(
        kind="structured",
        prompts=[_p("a"), _p("b")],
        composition=Composition(
            template="A: {{a}}\nB: {{b}}\n",
            when=None,
            fallback=None,
        ),
    )
    out = compose(s, {"a": "1", "b": "2"})
    assert "A: 1" in out
    assert "B: 2" in out


def test_compose_structured_when_true_uses_template():
    s = _slot(
        kind="structured",
        prompts=[_p("owns", kind="choice", choices=("yes", "no")), _p("model")],
        composition=Composition(
            template="Owns: {{model}}\n",
            when=Dependency("owns", "==", "yes"),
            fallback="No vehicle.\n",
        ),
    )
    out = compose(s, {"owns": "yes", "model": "GT3"})
    assert "Owns: GT3" in out
    assert "No vehicle" not in out


def test_compose_structured_when_false_uses_fallback():
    s = _slot(
        kind="structured",
        prompts=[_p("owns", kind="choice", choices=("yes", "no")), _p("model")],
        composition=Composition(
            template="Owns: {{model}}\n",
            when=Dependency("owns", "==", "yes"),
            fallback="No vehicle.\n",
        ),
    )
    out = compose(s, {"owns": "no"})
    assert out == "No vehicle.\n"


def test_compose_structured_no_composition_uses_default_block():
    s = _slot(
        kind="structured",
        prompts=[_p("a"), _p("b")],
        composition=None,
    )
    out = compose(s, {"a": "1", "b": "2"})
    assert "**a:** 1" in out
    assert "**b:** 2" in out


def test_compose_structured_skips_invisible_prompts_in_default():
    s = _slot(
        kind="structured",
        prompts=[
            _p("owns", kind="choice", choices=("yes", "no")),
            _p("model", depends_on=Dependency("owns", "==", "yes")),
        ],
        composition=None,
    )
    out = compose(s, {"owns": "no", "model": "should be hidden"})
    assert "should be hidden" not in out


# ---------------------------------------------------------------------------
# visible_prompts helper
# ---------------------------------------------------------------------------

def test_visible_prompts_filters_by_dependency():
    s = _slot(
        kind="structured",
        prompts=[
            _p("owns", kind="choice", choices=("yes", "no")),
            _p("model", depends_on=Dependency("owns", "==", "yes")),
            _p("year", depends_on=Dependency("owns", "==", "yes")),
        ],
        composition=None,
    )
    visible = visible_prompts(s, {"owns": "no"})
    assert [p.id for p in visible] == ["owns"]
    visible = visible_prompts(s, {"owns": "yes"})
    assert [p.id for p in visible] == ["owns", "model", "year"]
