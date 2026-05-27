"""Persona configuration loader.

Reads ``persona.yaml`` from the instance directory and parses the persona
configuration into a typed structure. ``persona.yaml`` is optional — an
absent file is equivalent to all-defaults (no fragments injected, no
behaviour change). This keeps the change backward-compatible with every
existing instance that has never heard of ``persona.yaml``.

Schema (Phase 1 — only the ``task_graph`` block is read):

    task_graph:
      participates: bool                    # default false
      preferred_status_path: str            # default "accept_then_work"

Extra top-level keys are accepted but ignored — the file is allowed to
grow other sections as future fragments are added. Extra keys inside
``task_graph`` are rejected so typos surface loudly.

Spec: ``docs/specs/persona-task-assigned.md`` §3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover — pyyaml is a hard dep in pyproject
    yaml = None  # type: ignore


PERSONA_YAML_FILENAME = "persona.yaml"

ALLOWED_STATUS_PATHS = ("accept_then_work",)
DEFAULT_STATUS_PATH = "accept_then_work"

_TASK_GRAPH_ALLOWED_KEYS = frozenset({"participates", "preferred_status_path"})


class PersonaConfigError(ValueError):
    """Raised when ``persona.yaml`` is present but malformed."""


@dataclass(frozen=True)
class TaskGraphConfig:
    """Opt-in flags for task-graph participation.

    ``participates`` gates whether the ``task_assigned`` fragment is
    appended to the persona system text. Default is False — absence of the
    block in ``persona.yaml`` (or absence of the file itself) means the
    persona stays unaware of the task-graph protocol.
    """

    participates: bool = False
    preferred_status_path: str = DEFAULT_STATUS_PATH


@dataclass(frozen=True)
class PersonaConfig:
    """Compiled persona configuration.

    Only ``task_graph`` is read today; other sections may join later. The
    loader returns a fully-defaulted instance when ``persona.yaml`` is
    missing, so callers can use the result unconditionally.
    """

    task_graph: TaskGraphConfig = TaskGraphConfig()


def load_persona_config(instance_dir: Path) -> PersonaConfig:
    """Return the persona config for ``instance_dir``.

    Missing file → all-defaults (no opt-in to anything).
    Malformed file → ``PersonaConfigError`` with a precise message.
    """
    persona_path = Path(instance_dir) / PERSONA_YAML_FILENAME
    if not persona_path.exists():
        return PersonaConfig()
    return load_persona_config_from_path(persona_path)


def load_persona_config_from_path(path: Path) -> PersonaConfig:
    """Parse ``persona.yaml`` at an explicit path. Used by tests."""
    if yaml is None:  # pragma: no cover — pyyaml is in pyproject deps.
        raise PersonaConfigError("pyyaml is required to load persona.yaml")

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PersonaConfigError(f"cannot read {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise PersonaConfigError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise PersonaConfigError(f"{path}: top-level must be a mapping")

    return PersonaConfig(task_graph=_parse_task_graph(data.get("task_graph")))


def _parse_task_graph(raw: Any) -> TaskGraphConfig:
    """Validate and coerce the ``task_graph`` block.

    Absent block → all-defaults. Present-but-empty mapping → same. The
    block is allowed to ship with only ``participates`` set; missing keys
    fall back to dataclass defaults.
    """
    if raw is None:
        return TaskGraphConfig()
    if not isinstance(raw, dict):
        raise PersonaConfigError("task_graph: must be a mapping")

    unknown = set(raw.keys()) - _TASK_GRAPH_ALLOWED_KEYS
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise PersonaConfigError(f"task_graph: unknown field(s): {joined}")

    participates = raw.get("participates", False)
    if not isinstance(participates, bool):
        raise PersonaConfigError("task_graph.participates: must be boolean")

    status_path = raw.get("preferred_status_path", DEFAULT_STATUS_PATH)
    if not isinstance(status_path, str) or not status_path.strip():
        raise PersonaConfigError(
            "task_graph.preferred_status_path: must be a non-empty string"
        )
    if status_path not in ALLOWED_STATUS_PATHS:
        joined = ", ".join(ALLOWED_STATUS_PATHS)
        raise PersonaConfigError(
            f"task_graph.preferred_status_path: must be one of {{{joined}}}"
        )

    return TaskGraphConfig(
        participates=participates,
        preferred_status_path=status_path,
    )
