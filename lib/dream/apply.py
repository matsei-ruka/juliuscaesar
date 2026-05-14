"""Apply or stage dream artifacts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import risk
from .schema import AppliedArtifact, ProposedArtifact


def apply_artifacts(
    instance_dir: Path,
    artifacts: list[ProposedArtifact],
    *,
    dry_run: bool = False,
) -> list[AppliedArtifact]:
    out: list[AppliedArtifact] = []
    for artifact in artifacts:
        classified = _with_risk(instance_dir, artifact)
        rejected = risk.rejected_by_frozen_guard(instance_dir, classified)
        if rejected:
            out.append(AppliedArtifact(classified, "REJECTED_SELF", rejected))
            continue
        if dry_run:
            out.append(AppliedArtifact(classified, "DRY_RUN"))
            continue
        if classified.risk_class == "SENSITIVE":
            _stage(instance_dir, classified)
            _raise_dream_approval(instance_dir, classified)
            out.append(AppliedArtifact(classified, "STAGED", "awaiting operator approval"))
            continue
        note = _write_artifact(instance_dir, classified)
        out.append(AppliedArtifact(classified, "AUTO_APPLIED", note))
    return out


def _raise_dream_approval(instance_dir: Path, artifact: ProposedArtifact) -> None:
    """Best-effort: register a SENSITIVE dream diff in the unified approvals table."""
    try:
        from approvals.service import find_by_source, raise_
    except Exception:
        return
    source_ref = f"dream:{artifact.diff_id}"
    if find_by_source(instance_dir, "dream", source_ref) is not None:
        return
    excerpt = (artifact.content or "")[:400]
    try:
        raise_(
            instance_dir,
            kind="dream_diff",
            title=f"{artifact.kind}: {artifact.title or artifact.path}",
            body=excerpt,
            payload={
                "diff_id": artifact.diff_id,
                "artifact_path": artifact.path,
                "artifact_kind": artifact.kind,
                "content_excerpt": excerpt,
                "risk_class": artifact.risk_class,
            },
            callback_payload={"diff_id": artifact.diff_id},
            producer="dream",
            source_ref=source_ref,
        )
    except Exception:
        return


def approve(instance_dir: Path, diff_id: str) -> Path:
    staged = _staged_path(instance_dir, diff_id)
    if not staged.exists():
        raise FileNotFoundError(f"staged dream diff not found: {diff_id}")
    data = json.loads(staged.read_text(encoding="utf-8"))
    artifact = ProposedArtifact(**data["artifact"])
    _write_artifact(instance_dir, artifact)
    applied = instance_dir / "state" / "dreams" / "approved" / f"{diff_id}.json"
    applied.parent.mkdir(parents=True, exist_ok=True)
    staged.replace(applied)
    return applied


def reject(instance_dir: Path, diff_id: str) -> str:
    staged = _staged_path(instance_dir, diff_id)
    if staged.exists():
        rejected = instance_dir / "state" / "dreams" / "rejected" / f"{diff_id}.json"
        rejected.parent.mkdir(parents=True, exist_ok=True)
        staged.replace(rejected)
        return "staged diff rejected"
    retain = _retain_path(instance_dir, diff_id)
    if retain.exists():
        data = json.loads(retain.read_text(encoding="utf-8"))
        target = instance_dir / data["path"]
        if data.get("existed"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(data.get("previous", ""), encoding="utf-8")
        else:
            target.unlink(missing_ok=True)
        rejected = instance_dir / "state" / "dreams" / "rejected" / f"{diff_id}.json"
        rejected.parent.mkdir(parents=True, exist_ok=True)
        retain.replace(rejected)
        return "auto-applied diff rolled back"
    raise FileNotFoundError(f"dream diff not found: {diff_id}")


def pending(instance_dir: Path) -> list[Path]:
    root = instance_dir / "state" / "dreams" / "pending"
    return sorted(root.glob("*.json")) if root.exists() else []


def _with_risk(instance_dir: Path, artifact: ProposedArtifact) -> ProposedArtifact:
    classified = risk.classify(instance_dir, artifact)
    if classified == artifact.risk_class:
        return artifact
    return ProposedArtifact(
        diff_id=artifact.diff_id,
        kind=artifact.kind,
        risk_class=classified,
        path=artifact.path,
        title=artifact.title,
        content=artifact.content,
        source_signals=artifact.source_signals,
        metadata=artifact.metadata,
    )


def _write_artifact(instance_dir: Path, artifact: ProposedArtifact) -> str:
    target = instance_dir / artifact.path
    if target.exists() and artifact.kind in {"playbook", "learning", "stub"}:
        return "already exists"
    _retain(instance_dir, artifact, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(artifact.content, encoding="utf-8")
    return f"wrote {artifact.path}"


def _stage(instance_dir: Path, artifact: ProposedArtifact) -> Path:
    path = _staged_path(instance_dir, artifact.diff_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"artifact": artifact.to_dict()}, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _retain(instance_dir: Path, artifact: ProposedArtifact, target: Path) -> None:
    path = _retain_path(instance_dir, artifact.diff_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "artifact": artifact.to_dict(),
        "path": artifact.path,
        "existed": target.exists(),
        "previous": target.read_text(encoding="utf-8", errors="replace") if target.exists() else "",
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    shutil.move(str(tmp), str(path))


def _staged_path(instance_dir: Path, diff_id: str) -> Path:
    return instance_dir / "state" / "dreams" / "pending" / f"{diff_id}.json"


def _retain_path(instance_dir: Path, diff_id: str) -> Path:
    return instance_dir / "state" / "dreams" / "retained" / f"{diff_id}.json"
