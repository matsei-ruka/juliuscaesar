"""Persona compiler — assembles per-agent system text from opt-in fragments.

This package owns the small set of brain-internal protocol fragments that
get injected into a persona's system prompt when the persona opts in via
``persona.yaml``. Today there is exactly one fragment — ``task_assigned``
— which teaches the persona how to handle ``company.task_assigned`` events
delivered through the company-inbox channel.

Spec: ``docs/specs/persona-task-assigned.md``.

Two surfaces:

* ``loader.load_persona_config(instance_dir)`` — read ``persona.yaml`` and
  return a typed config with sane defaults. Existing instances without
  ``persona.yaml`` get the all-false default block (no fragments injected).
* ``compiler.compile_fragments(persona_config)`` — return the concatenated
  fragment text for the fragments the persona opted into. Returns ``""``
  when nothing is opted in, so the caller can append unconditionally.
"""

from __future__ import annotations

from .compiler import compile_fragments
from .loader import PersonaConfig, TaskGraphConfig, load_persona_config

__all__ = [
    "PersonaConfig",
    "TaskGraphConfig",
    "load_persona_config",
    "compile_fragments",
]
