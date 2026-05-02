"""Canonical parser/formatter for brain specifications.

A brain spec is `<brain>` or `<brain>:<model>` (e.g. `claude`,
`codex:gpt-5.4-mini`, `claude:opus-4-7-1m`). Used wherever config or events
select a brain: `default_brain`, `channels.<name>.brain`,
`event.meta.brain_override`, cron `event.meta.brain`, alias resolutions.

Empty input yields `BrainSpec("", None)`. Whitespace is stripped from both
fields. A trailing `:` with no model also yields `model=None`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BrainSpec:
    brain: str
    model: str | None = None

    def format(self) -> str:
        return f"{self.brain}:{self.model}" if self.model else self.brain


def parse(spec: str | None) -> BrainSpec:
    if not spec:
        return BrainSpec("", None)
    text = spec.strip()
    if not text:
        return BrainSpec("", None)
    if ":" in text:
        brain, _, model = text.partition(":")
        return BrainSpec(brain.strip(), model.strip() or None)
    return BrainSpec(text, None)
