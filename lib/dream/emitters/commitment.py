"""Commitment artifact emitter."""

from __future__ import annotations

from datetime import timedelta

from commitments.schema import Commitment, format_datetime

from dream.ids import diff_id, slugify
from dream.schema import ProposedArtifact, StaleEntry


def emit_for_stale(stale_entries: list[StaleEntry], *, due_base) -> list[ProposedArtifact]:
    artifacts: list[ProposedArtifact] = []
    for stale in stale_entries[:5]:
        slug = slugify("verify-" + stale.path.replace("/", "-").replace(".md", ""), max_len=54)
        due = due_base + timedelta(hours=24)
        commitment = Commitment(
            slug=slug,
            created_at=due_base,
            due_at=due,
            action="jc-event",
            text=f"Verify stale memory entry: {stale.path}",
            tags=("dream", "verify-this", "memory-hygiene"),
            origin="dream",
            metadata={"retries": 0, "source_path": stale.path, "reason": stale.reason},
        )
        content = _to_yamlish(commitment)
        path = f"state/commitments/{slug}.yaml"
        artifacts.append(
            ProposedArtifact(
                diff_id=diff_id("commitment", path, content),
                kind="commitment",
                risk_class="MEDIUM",
                path=path,
                title=f"Verify {stale.path}",
                content=content,
                metadata={"due_at": format_datetime(due)},
            )
        )
    return artifacts


def _to_yamlish(commitment: Commitment) -> str:
    from commitments.schema import to_dict
    import yaml

    return yaml.safe_dump(to_dict(commitment), sort_keys=False, default_flow_style=False)
