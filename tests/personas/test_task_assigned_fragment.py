"""Tests for the ``task_assigned`` persona fragment plumbing.

Covers the three test goals from
``docs/specs/persona-task-assigned.md`` §8:

1. Snapshot of compiled persona text — fragment present when
   ``task_graph.participates: true``; absent otherwise.
2. Cross-persona consistency — three distinct personas all yield
   byte-identical fragment text (it is static, no per-persona
   substitution).
3. Backward compatibility — a persona without a ``task_graph`` block (or
   without ``persona.yaml`` at all) loads cleanly and defaults to
   ``participates: false``.

Plus a few schema-validation tests to catch typo/coercion regressions.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make ``lib/`` importable so ``personas`` resolves as a top-level package.
_LIB = Path(__file__).resolve().parent.parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from personas import compile_fragments, load_persona_config  # noqa: E402
from personas.compiler import FRAGMENTS_DIR, TASK_ASSIGNED_FRAGMENT  # noqa: E402
from personas.loader import (  # noqa: E402
    PersonaConfig,
    PersonaConfigError,
    TaskGraphConfig,
    load_persona_config_from_path,
)


# A short, distinctive phrase from the fragment body. If the fragment is
# rewritten this assertion will fail — that is intentional, the test
# wants to notice if the protocol prose accidentally disappears.
FRAGMENT_MARKER = "Task assignments via company-inbox"
FSM_MARKER = "illegal_transition"
SILENT_TASK_MARKER = "silencing the task"


def _read_fragment_text() -> str:
    return (FRAGMENTS_DIR / TASK_ASSIGNED_FRAGMENT).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Snapshot test — opt-in toggle controls fragment presence.
# ---------------------------------------------------------------------------

def test_fragment_included_when_participates_true(tmp_path: Path):
    persona_yaml = tmp_path / "persona.yaml"
    persona_yaml.write_text(
        "task_graph:\n  participates: true\n",
        encoding="utf-8",
    )

    cfg = load_persona_config(tmp_path)
    out = compile_fragments(cfg)

    assert FRAGMENT_MARKER in out
    assert FSM_MARKER in out
    assert SILENT_TASK_MARKER in out


def test_fragment_omitted_when_participates_false(tmp_path: Path):
    persona_yaml = tmp_path / "persona.yaml"
    persona_yaml.write_text(
        "task_graph:\n  participates: false\n",
        encoding="utf-8",
    )

    cfg = load_persona_config(tmp_path)
    out = compile_fragments(cfg)

    assert out == ""


def test_fragment_omitted_when_task_graph_block_absent(tmp_path: Path):
    """A persona.yaml present but with no task_graph block is the same as
    participates=false. The fragment must NOT appear."""
    persona_yaml = tmp_path / "persona.yaml"
    persona_yaml.write_text("# empty config\n", encoding="utf-8")

    cfg = load_persona_config(tmp_path)
    out = compile_fragments(cfg)

    assert out == ""
    assert cfg.task_graph.participates is False


# ---------------------------------------------------------------------------
# 2. Cross-persona consistency — static fragment, no per-persona substitution.
# ---------------------------------------------------------------------------

def test_three_personas_emit_byte_identical_fragment(tmp_path: Path):
    """Compile three personas with different slugs / display names but the
    same opt-in. The fragment text must be byte-identical across all three.
    """
    personas = [
        ("sergio", "Sergio Ricci"),
        ("ethan",  "Ethan Hill"),
        ("penelope", "Penelope Vance"),
    ]
    compiled: list[str] = []
    for slug, display_name in personas:
        agent_dir = tmp_path / slug
        agent_dir.mkdir()
        (agent_dir / "persona.yaml").write_text(
            f"# persona for {display_name}\n"
            f"task_graph:\n"
            f"  participates: true\n",
            encoding="utf-8",
        )
        cfg = load_persona_config(agent_dir)
        compiled.append(compile_fragments(cfg))

    assert compiled[0] == compiled[1] == compiled[2]
    assert FRAGMENT_MARKER in compiled[0]


# ---------------------------------------------------------------------------
# 3. Backward compatibility — existing personas without persona.yaml.
# ---------------------------------------------------------------------------

def test_missing_persona_yaml_defaults_to_no_participation(tmp_path: Path):
    """An instance with no persona.yaml at all (the common case today)
    must load without error and produce the empty compiled output."""
    # tmp_path is empty — no persona.yaml.
    cfg = load_persona_config(tmp_path)
    assert isinstance(cfg, PersonaConfig)
    assert cfg.task_graph.participates is False
    assert cfg.task_graph.preferred_status_path == "accept_then_work"
    assert compile_fragments(cfg) == ""


def test_existing_instance_preamble_byte_identical(tmp_path: Path):
    """Compile the gateway preamble before and after the persona.yaml
    integration to confirm a real existing instance (no persona.yaml,
    nothing opted in) gets byte-identical output.

    We don't have the full memory tree here, so we drive the test through
    ``render_persona_fragments_block`` directly — it is the only new code
    path inserted into ``render_preamble``.
    """
    # ``lib/`` and ``lib/gateway/`` are already on sys.path via conftest.py.
    from gateway.context import render_persona_fragments_block

    assert render_persona_fragments_block(tmp_path) == ""


# ---------------------------------------------------------------------------
# Gateway integration — render_preamble pickup of the opt-in.
# ---------------------------------------------------------------------------

def _make_minimal_instance(root: Path, persona_yaml: str | None = None) -> Path:
    """Build the smallest instance dir render_preamble accepts."""
    l1 = root / "memory" / "L1"
    l1.mkdir(parents=True)
    (l1 / "IDENTITY.md").write_text("# Identity\nTest agent.\n", encoding="utf-8")
    if persona_yaml is not None:
        (root / "persona.yaml").write_text(persona_yaml, encoding="utf-8")
    return root


def test_render_preamble_includes_fragment_when_opted_in(tmp_path: Path):
    """End-to-end: render_preamble pulls the fragment text in for an
    opted-in persona, and omits it otherwise."""
    from gateway import context

    context.clear_cache()
    opted_in = _make_minimal_instance(
        tmp_path / "opted_in",
        persona_yaml="task_graph:\n  participates: true\n",
    )
    opted_out = _make_minimal_instance(
        tmp_path / "opted_out",
        persona_yaml="task_graph:\n  participates: false\n",
    )
    no_yaml = _make_minimal_instance(tmp_path / "no_yaml", persona_yaml=None)

    preamble_in = context.render_preamble(opted_in)
    context.clear_cache()
    preamble_out = context.render_preamble(opted_out)
    context.clear_cache()
    preamble_none = context.render_preamble(no_yaml)

    assert FRAGMENT_MARKER in preamble_in
    assert FRAGMENT_MARKER not in preamble_out
    assert FRAGMENT_MARKER not in preamble_none


# ---------------------------------------------------------------------------
# Schema validation — typos and bad values should fail loud.
# ---------------------------------------------------------------------------

def test_unknown_task_graph_key_rejected(tmp_path: Path):
    persona_yaml = tmp_path / "persona.yaml"
    persona_yaml.write_text(
        "task_graph:\n  participatez: true\n",  # typo
        encoding="utf-8",
    )
    with pytest.raises(PersonaConfigError, match="unknown field"):
        load_persona_config(tmp_path)


def test_non_boolean_participates_rejected(tmp_path: Path):
    persona_yaml = tmp_path / "persona.yaml"
    persona_yaml.write_text(
        'task_graph:\n  participates: "yes"\n',
        encoding="utf-8",
    )
    with pytest.raises(PersonaConfigError, match="must be boolean"):
        load_persona_config(tmp_path)


def test_unsupported_status_path_rejected(tmp_path: Path):
    persona_yaml = tmp_path / "persona.yaml"
    persona_yaml.write_text(
        "task_graph:\n"
        "  participates: true\n"
        "  preferred_status_path: skip_acceptance\n",
        encoding="utf-8",
    )
    with pytest.raises(PersonaConfigError, match="preferred_status_path"):
        load_persona_config(tmp_path)


def test_extra_top_level_keys_tolerated(tmp_path: Path):
    """The schema is open at the top level — future fragments will add new
    blocks alongside ``task_graph`` and this PR must not block them."""
    persona_yaml = tmp_path / "persona.yaml"
    persona_yaml.write_text(
        "task_graph:\n"
        "  participates: true\n"
        "future_block:\n"
        "  something: 1\n",
        encoding="utf-8",
    )
    cfg = load_persona_config(tmp_path)
    assert cfg.task_graph.participates is True


# ---------------------------------------------------------------------------
# Round-trip: explicit path loader works the same as instance_dir loader.
# ---------------------------------------------------------------------------

def test_explicit_path_loader_matches_instance_dir_loader(tmp_path: Path):
    persona_yaml = tmp_path / "persona.yaml"
    persona_yaml.write_text(
        "task_graph:\n  participates: true\n",
        encoding="utf-8",
    )
    via_dir = load_persona_config(tmp_path)
    via_path = load_persona_config_from_path(persona_yaml)
    assert via_dir == via_path == PersonaConfig(
        task_graph=TaskGraphConfig(participates=True),
    )
