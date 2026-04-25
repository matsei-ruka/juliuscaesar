"""Triage backend ABC and shared result type."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


CLASSES = (
    "smalltalk",
    "quick",
    "analysis",
    "code",
    "image",
    "voice",
    "system",
    "unsafe",
)


PromptDir = Path(__file__).resolve().parent
PROMPT_PATH = PromptDir / "prompt.md"


@dataclass(frozen=True)
class TriageResult:
    class_: str
    brain: str
    confidence: float
    reasoning: str | None = None
    raw: str | None = None

    def is_unsafe(self) -> bool:
        return self.class_ == "unsafe"


def load_prompt_template() -> str:
    if not PROMPT_PATH.exists():
        return ""
    return PROMPT_PATH.read_text(encoding="utf-8")


def render_prompt(message: str) -> str:
    template = load_prompt_template() or _DEFAULT_PROMPT
    return template.replace("{message}", message)


_JSON_LINE_RE = re.compile(r"\{.*\}")


def parse_triage_json(raw: str) -> TriageResult | None:
    """Find the first single-line JSON object in `raw` and parse it."""
    if not raw:
        return None
    match = _JSON_LINE_RE.search(raw)
    if not match:
        return None
    blob = match.group(0)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    cls = str(data.get("class") or "").strip()
    brain = str(data.get("brain") or "").strip()
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if not cls or not brain:
        return None
    return TriageResult(
        class_=cls if cls in CLASSES else "quick",
        brain=brain,
        confidence=max(0.0, min(1.0, confidence)),
        reasoning=str(data.get("reasoning") or "") or None,
        raw=blob,
    )


class TriageBackend:
    name: str = "base"

    def classify(self, message: str) -> TriageResult:  # pragma: no cover - abstract
        raise NotImplementedError


_DEFAULT_PROMPT = """You are a triage classifier. You output exactly one JSON
object on a single line.

Schema: {"class":"<class>","brain":"<brain>","confidence":<0..1>}

Classify this message:
{message}
"""
