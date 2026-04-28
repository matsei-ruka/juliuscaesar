"""Configuration for autonomous user model updates."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None


@dataclass(frozen=True)
class PrivacyFilterConfig:
    llm_pass: bool = False
    llm_model: str = "claude-haiku-4-5"


@dataclass(frozen=True)
class DetectorsConfig:
    recurring_topic: bool = True
    comm_pref: bool = True
    priority_shift: bool = True
    new_entity: bool = True
    rule_drift: bool = True


@dataclass(frozen=True)
class UserModelConfig:
    enabled: bool = False
    apply_mode: str = "propose"  # disabled | propose | auto_high_confidence | auto_all
    cadence_cron: str = "0 3 * * *"
    look_back_days: int = 7
    min_evidence_count: int = 3
    confidence_threshold: float = 0.85
    proposal_cooldown_days: int = 30
    notify_chat_id: str | None = None
    proposer_model: str = "claude-sonnet-4-6"
    privacy_filter: PrivacyFilterConfig = field(default_factory=PrivacyFilterConfig)
    detectors: DetectorsConfig = field(default_factory=DetectorsConfig)


def load_config(instance_dir: Path) -> UserModelConfig:
    """Load config from ops/user_model.yaml. Returns default disabled config if file missing."""
    cfg_path = instance_dir / "ops" / "user_model.yaml"
    if not cfg_path.exists():
        return UserModelConfig()
    if yaml is None:
        raise ImportError("PyYAML required to load ops/user_model.yaml")
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return UserModelConfig()
    return _build_config(data)


def _build_config(data: dict) -> UserModelConfig:
    """Hydrate UserModelConfig from dict, with defaults."""
    privacy_filter_data = data.get("privacy_filter") or {}
    privacy = PrivacyFilterConfig(
        llm_pass=privacy_filter_data.get("llm_pass", False),
        llm_model=privacy_filter_data.get("llm_model", "claude-haiku-4-5"),
    )

    detectors_data = data.get("detectors") or {}
    detectors = DetectorsConfig(
        recurring_topic=detectors_data.get("recurring_topic", True),
        comm_pref=detectors_data.get("comm_pref", True),
        priority_shift=detectors_data.get("priority_shift", True),
        new_entity=detectors_data.get("new_entity", True),
        rule_drift=detectors_data.get("rule_drift", True),
    )

    return UserModelConfig(
        enabled=data.get("enabled", False),
        apply_mode=data.get("apply_mode", "propose"),
        cadence_cron=data.get("cadence_cron", "0 3 * * *"),
        look_back_days=data.get("look_back_days", 7),
        min_evidence_count=data.get("min_evidence_count", 3),
        confidence_threshold=data.get("confidence_threshold", 0.85),
        proposal_cooldown_days=data.get("proposal_cooldown_days", 30),
        notify_chat_id=data.get("notify_chat_id"),
        proposer_model=data.get("proposer_model", "claude-sonnet-4-6"),
        privacy_filter=privacy,
        detectors=detectors,
    )
