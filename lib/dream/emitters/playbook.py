"""Playbook artifact emitter."""

from __future__ import annotations

from datetime import date

from self_model.detector import Signal

from dream.ids import diff_id, slugify
from dream.schema import ProposedArtifact, Reflection


def emit(reflection: Reflection, signals: list[Signal]) -> list[ProposedArtifact]:
    artifacts: list[ProposedArtifact] = []
    useful = [s for s in signals if s.kind in {"episode_flag", "direct_request", "hot_flag"}]
    for signal in useful[:3]:
        title = f"Pattern from {signal.kind.replace('_', ' ')}"
        slug = slugify(title + "-" + (signal.trigger or "signal"), fallback="playbook")
        path = f"memory/L2/playbooks/{slug}.md"
        today = date.today().isoformat()
        content = f"""---
slug: playbooks/{slug}
title: {title}
layer: L2
type: playbook
state: draft
created: {today}
updated: {today}
last_verified: {today}
provenance: dream/{reflection.window_end.isoformat()}
tags: [playbook, dream]
trigger: {signal.trigger}
links: []
---

# {title}

## When to use
Use this when a future conversation resembles this signal: {signal.trigger}.

## Procedure
1. Re-read the relevant transcript or memory note before acting.
2. State the observed pattern plainly.
3. Choose the smallest durable follow-through: answer, memory update, or commitment.

## Anti-patterns
- Do not treat one signal as a permanent rule.
- Do not overwrite L1 doctrine from a playbook.

## Source dreams
- dream/{reflection.window_end.isoformat()} — {signal.text_excerpt[:160]}
"""
        artifacts.append(
            ProposedArtifact(
                diff_id=diff_id("playbook", path, content),
                kind="playbook",
                risk_class="MEDIUM",
                path=path,
                title=title,
                content=content,
                source_signals=(signal.kind,),
            )
        )
    return artifacts
