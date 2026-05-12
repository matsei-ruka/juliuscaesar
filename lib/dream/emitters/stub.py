"""Broken backlink stub emitter."""

from __future__ import annotations

from datetime import date

from dream.ids import diff_id, slugify
from dream.schema import BrokenLink, ProposedArtifact, Reflection


def emit(reflection: Reflection, links: list[BrokenLink]) -> list[ProposedArtifact]:
    artifacts: list[ProposedArtifact] = []
    today = date.today().isoformat()
    for link in links[:10]:
        slug = slugify(link.target, fallback="stub")
        path = f"memory/L2/stubs/{slug}.md"
        content = f"""---
slug: stubs/{slug}
title: {link.target}
layer: L2
type: stub
state: stub
created: {today}
updated: {today}
last_verified: ""
provenance: dream/{reflection.window_end.isoformat()}
tags: [stub, dream]
links: []
---

# {link.target}

Stub created because `{link.source}` linked to `[[{link.target}]]`.

Context: {link.context}
"""
        artifacts.append(
            ProposedArtifact(
                diff_id=diff_id("stub", path, content),
                kind="stub",
                risk_class="LOW",
                path=path,
                title=f"Stub for {link.target}",
                content=content,
            )
        )
    return artifacts
