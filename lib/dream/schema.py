"""Dataclasses for the dream pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal


RiskClass = Literal["LOW", "MEDIUM", "SENSITIVE"]
ArtifactKind = Literal[
    "memory_diff",
    "playbook",
    "learning",
    "commitment",
    "rules_proposal",
    "identity_proposal",
    "stub",
]


@dataclass(frozen=True)
class TranscriptDelta:
    conversation_id: str
    path: str
    events: int
    first_ts: str | None = None
    last_ts: str | None = None


@dataclass(frozen=True)
class SentRecord:
    ts: str
    body_preview: str


@dataclass(frozen=True)
class CommitmentRecord:
    slug: str
    path: str
    ts: str | None = None


@dataclass(frozen=True)
class Reflection:
    window_start: datetime
    window_end: datetime
    transcript_deltas: list[TranscriptDelta] = field(default_factory=list)
    memory_state_hash: str = ""
    sent_deltas: list[SentRecord] = field(default_factory=list)
    closed_commitments: list[CommitmentRecord] = field(default_factory=list)


@dataclass(frozen=True)
class DuplicateGroup:
    paths: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class BrokenLink:
    source: str
    target: str
    context: str = ""


@dataclass(frozen=True)
class StaleEntry:
    path: str
    last_verified: str
    reason: str


@dataclass(frozen=True)
class ConsolidationFindings:
    signals: list[Any] = field(default_factory=list)
    duplicates: list[DuplicateGroup] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    broken_backlinks: list[BrokenLink] = field(default_factory=list)
    stale_timestamps: list[StaleEntry] = field(default_factory=list)


@dataclass(frozen=True)
class ProposedArtifact:
    diff_id: str
    kind: ArtifactKind
    risk_class: RiskClass
    path: str
    title: str
    content: str
    source_signals: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AppliedArtifact:
    artifact: ProposedArtifact
    status: Literal["AUTO_APPLIED", "STAGED", "REJECTED_SELF", "DRY_RUN"]
    note: str = ""


@dataclass(frozen=True)
class DreamResult:
    dream_id: str
    report_path: Path | None
    reflection: Reflection
    findings: ConsolidationFindings
    artifacts: list[AppliedArtifact]
    status: str
