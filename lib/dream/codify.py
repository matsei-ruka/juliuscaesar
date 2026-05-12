"""Codify phase: turn findings into proposed artifacts."""

from __future__ import annotations

from .emitters import commitment, learning, playbook, stub
from .schema import ConsolidationFindings, ProposedArtifact, Reflection


def codify(reflection: Reflection, findings: ConsolidationFindings) -> list[ProposedArtifact]:
    artifacts: list[ProposedArtifact] = []
    artifacts.extend(stub.emit(reflection, findings.broken_backlinks))
    artifacts.extend(playbook.emit(reflection, findings.signals))
    artifacts.extend(learning.emit(reflection, findings.signals))
    artifacts.extend(commitment.emit_for_stale(findings.stale_timestamps, due_base=reflection.window_end))
    return _dedupe(artifacts)


def _dedupe(artifacts: list[ProposedArtifact]) -> list[ProposedArtifact]:
    seen: set[str] = set()
    out: list[ProposedArtifact] = []
    for artifact in artifacts:
        if artifact.diff_id in seen:
            continue
        seen.add(artifact.diff_id)
        out.append(artifact)
    return out
