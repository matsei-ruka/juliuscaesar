"""Persona interview engine — typed loader + (later) gap detection + splice.

Phase 3 deliverable: `questions` module with the YAML-backed Slot/Prompt
dataclasses + validation. Phase 5 will add `gaps`, `engine`, `cli` for the
end-to-end `jc persona interview` flow.
"""

from .compose import compose, visible_prompts
from .engine import (
    InterviewResult,
    Prompter,
    bind_macros_in_instance,
    interview,
)
from .gaps import Gap, GapState, classify_slot, find_gaps, summarize
from .questions import (
    Composition,
    Prompt,
    QuestionsBank,
    Slot,
    Validation,
    load_questions,
)
from .splice import SpliceError, splice_slot_body

__all__ = [
    "Composition",
    "Gap",
    "GapState",
    "InterviewResult",
    "Prompt",
    "Prompter",
    "QuestionsBank",
    "Slot",
    "SpliceError",
    "Validation",
    "bind_macros_in_instance",
    "classify_slot",
    "compose",
    "find_gaps",
    "interview",
    "load_questions",
    "splice_slot_body",
    "summarize",
    "visible_prompts",
]
