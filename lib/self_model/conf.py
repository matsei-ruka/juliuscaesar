"""Configuration for autonomous self model updates."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None


@dataclass(frozen=True)
class DetectorsConfig:
    filippo_correction: bool = False
    hot_flag: bool = False
    direct_request: bool = False
    episode_flag: bool = False
    scan_weekly: bool = False


@dataclass(frozen=True)
class SelfModelConfig:
    enabled: bool = False
    mode: str = "dry_run"  # dry_run | propose | apply
    require_dkim_for_rules: bool = True
    require_dkim_for_identity: bool = True
    require_dkim_for_journal: bool = False
    scan_weekly_cron: str = "0 9 * * 0"
    look_back_days: int = 7
    min_evidence_count: int = 2
    confidence_threshold: float = 0.85
    proposal_cooldown_days: int = 30
    notify_chat_id: str | None = None
    proposer_model: str = "claude-sonnet-4-6"
    detectors: DetectorsConfig = field(default_factory=DetectorsConfig)


def load_config(instance_dir: Path) -> SelfModelConfig:
    """Load config from ops/self_model.yaml. Returns default disabled config if file missing."""
    cfg_path = instance_dir / "ops" / "self_model.yaml"
    if not cfg_path.exists():
        return SelfModelConfig()
    if yaml is None:
        raise ImportError("PyYAML required to load ops/self_model.yaml")
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return SelfModelConfig()
    return _build_config(data)


def _build_config(data: dict) -> SelfModelConfig:
    """Hydrate SelfModelConfig from dict, with defaults."""
    detectors_data = data.get("detectors") or {}
    detectors = DetectorsConfig(
        filippo_correction=detectors_data.get("filippo_correction", False),
        hot_flag=detectors_data.get("hot_flag", False),
        direct_request=detectors_data.get("direct_request", False),
        episode_flag=detectors_data.get("episode_flag", False),
        scan_weekly=detectors_data.get("scan_weekly", False),
    )

    return SelfModelConfig(
        enabled=data.get("enabled", False),
        mode=data.get("mode", "dry_run"),
        require_dkim_for_rules=data.get("require_dkim_for_rules", True),
        require_dkim_for_identity=data.get("require_dkim_for_identity", True),
        require_dkim_for_journal=data.get("require_dkim_for_journal", False),
        scan_weekly_cron=data.get("scan_weekly_cron", "0 9 * * 0"),
        look_back_days=data.get("look_back_days", 7),
        min_evidence_count=data.get("min_evidence_count", 2),
        confidence_threshold=data.get("confidence_threshold", 0.85),
        proposal_cooldown_days=data.get("proposal_cooldown_days", 30),
        notify_chat_id=data.get("notify_chat_id"),
        proposer_model=data.get("proposer_model", "claude-sonnet-4-6"),
        detectors=detectors,
    )
