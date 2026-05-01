"""Persona interview engine — typed loader + (later) gap detection + splice.

Phase 3 deliverable: `questions` module with the YAML-backed Slot/Prompt
dataclasses + validation. Phase 5 will add `gaps`, `engine`, `cli` for the
end-to-end `jc persona interview` flow.
"""

from .questions import (
    Composition,
    Prompt,
    QuestionsBank,
    Slot,
    Validation,
    load_questions,
)

__all__ = [
    "Composition",
    "Prompt",
    "QuestionsBank",
    "Slot",
    "Validation",
    "load_questions",
]
