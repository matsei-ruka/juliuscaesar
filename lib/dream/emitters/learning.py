"""Learning artifact emitter."""

from __future__ import annotations

from datetime import date

from self_model.detector import Signal

from dream.ids import diff_id, slugify
from dream.schema import ProposedArtifact, Reflection


def emit(reflection: Reflection, signals: list[Signal]) -> list[ProposedArtifact]:
    artifacts: list[ProposedArtifact] = []
    for signal in [s for s in signals if s.kind in {"filippo_correction", "direct_request"}][:3]:
        title = f"Learning from {signal.kind.replace('_', ' ')}"
        slug = slugify(title + "-" + signal.trigger, fallback="learning")
        path = f"memory/L2/learnings/{slug}.md"
        today = date.today().isoformat()
        content = f"""---
slug: learnings/{slug}
title: {title}
layer: L2
type: learning
state: draft
created: {today}
updated: {today}
last_verified: {today}
provenance: dream/{reflection.window_end.isoformat()}
tags: [learning, dream]
links: []
---

# {title}

## Observation
{signal.text_excerpt}

## Operating implication
Before acting in a similar context, verify the real state and choose the
smallest durable change that prevents repeat drift.

## Source dreams
- dream/{reflection.window_end.isoformat()} — {signal.kind}:{signal.trigger}
"""
        artifacts.append(
            ProposedArtifact(
                diff_id=diff_id("learning", path, content),
                kind="learning",
                risk_class="MEDIUM",
                path=path,
                title=title,
                content=content,
                source_signals=(signal.kind,),
            )
        )
    return artifacts
