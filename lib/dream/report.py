"""Dream report rendering."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .schema import AppliedArtifact, ConsolidationFindings, Reflection


def write_report(
    instance_dir: Path,
    *,
    dream_id: str,
    reflection: Reflection,
    findings: ConsolidationFindings,
    artifacts: list[AppliedArtifact],
    status: str,
    dry_run: bool = False,
) -> Path | None:
    if dry_run:
        return None
    root = instance_dir / "state" / "dreams"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{dream_id}.md"
    path.write_text(
        render_report(
            dream_id=dream_id,
            reflection=reflection,
            findings=findings,
            artifacts=artifacts,
            status=status,
        ),
        encoding="utf-8",
    )
    return path


def render_report(
    *,
    dream_id: str,
    reflection: Reflection,
    findings: ConsolidationFindings,
    artifacts: list[AppliedArtifact],
    status: str,
) -> str:
    lines = [
        "---",
        f"dream_id: {dream_id}",
        f'window_start: "{reflection.window_start.isoformat()}"',
        f'window_end: "{reflection.window_end.isoformat()}"',
        f"generated_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"status: {status}",
        "artifacts:",
        f"  total: {len(artifacts)}",
        "---",
        "",
        f"# Dream {dream_id}",
        "",
        "## Reflection summary",
        f"- Transcript deltas: {len(reflection.transcript_deltas)}",
        f"- Memory state hash: {reflection.memory_state_hash}",
        f"- Sent records: {len(reflection.sent_deltas)}",
        f"- Closed commitments: {len(reflection.closed_commitments)}",
        "",
        "## Consolidation findings",
        f"- Signals: {len(findings.signals)}",
        f"- Duplicates: {len(findings.duplicates)}",
        f"- Contradictions: {len(findings.contradictions)}",
        f"- Broken backlinks: {len(findings.broken_backlinks)}",
        f"- Stale timestamps: {len(findings.stale_timestamps)}",
        "",
        "## Artifacts emitted",
    ]
    if not artifacts:
        lines.append("- none")
    for applied in artifacts:
        lines.append(
            f"- {applied.status}: {applied.artifact.diff_id} "
            f"{applied.artifact.kind} {applied.artifact.path} "
            f"[{applied.artifact.risk_class}] {applied.note}"
        )
    return "\n".join(lines) + "\n"
