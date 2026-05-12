"""Risk classification for dream artifacts."""

from __future__ import annotations

from pathlib import Path

from self_model import frozen_sections

from .schema import ProposedArtifact, RiskClass


def classify(instance_dir: Path, artifact: ProposedArtifact) -> RiskClass:
    path = Path(artifact.path)
    if "memory/L1" in path.as_posix() or path.name in {"RULES.md", "IDENTITY.md", "STYLE.md", "USER.md"}:
        return "SENSITIVE"
    if artifact.kind in {"playbook", "learning", "commitment"}:
        return "MEDIUM"
    return artifact.risk_class


def rejected_by_frozen_guard(instance_dir: Path, artifact: ProposedArtifact) -> str | None:
    target_section = str(artifact.metadata.get("target_section") or "")
    if target_section and frozen_sections.is_section_frozen(artifact.path, target_section):
        return f"section is IMMUTABILE: {target_section}"
    target = instance_dir / artifact.path
    if target.exists() and "<!-- FROZEN -->" in target.read_text(encoding="utf-8", errors="replace"):
        return "target contains FROZEN marker"
    return None
