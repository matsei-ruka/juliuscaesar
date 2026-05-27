"""Persona fragment compiler.

Given a ``PersonaConfig``, return the concatenated text of every fragment
the persona has opted into. Today there is one fragment
(``task_assigned``); the compiler is shaped so adding more is mechanical.

The fragment files live at ``lib/personas/fragments/*.md.j2``. The ``.j2``
suffix is forward-looking — fragments today have no template substitutions
and are read as plain text. When a fragment grows ``{{macro}}``
substitutions, switch to ``persona_macros.bind_macros`` here; the wire
format and call sites stay the same.

Spec: ``docs/specs/persona-task-assigned.md`` §6.
"""

from __future__ import annotations

from pathlib import Path

from .loader import PersonaConfig


FRAGMENTS_DIR = Path(__file__).resolve().parent / "fragments"
TASK_ASSIGNED_FRAGMENT = "task_assigned.md.j2"


def compile_fragments(persona: PersonaConfig) -> str:
    """Return concatenated opt-in fragment text. ``""`` when nothing opts in.

    Order matches the order fragments are added below — today there is
    only one, so order is moot. When a second fragment lands, follow the
    rule from the spec: protocol guidance after core behaviour, before
    persona-specific overrides. The caller is responsible for splicing
    this output into the right place in the system prompt.
    """
    parts: list[str] = []
    if persona.task_graph.participates:
        parts.append(_read_fragment(TASK_ASSIGNED_FRAGMENT))
    return "\n\n".join(part.strip() for part in parts if part.strip())


def _read_fragment(name: str) -> str:
    """Read a fragment file by name. Trusts the bundled file exists.

    Fragments ship with the package; a missing file is a packaging bug,
    not an operator-recoverable error. We surface it as ``FileNotFoundError``
    so the gateway logs are loud rather than silently degrading.
    """
    path = FRAGMENTS_DIR / name
    return path.read_text(encoding="utf-8")
