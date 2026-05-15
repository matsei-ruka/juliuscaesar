"""Adaptive discovery health checks consumed by `jc-doctor`.

Covers docs/specs/adaptive-discovery.md §Phase 5. Returns a list of
`HealthItem` records the shell doctor renders with the standard ok/warn/
fail/info glyphs.

When `adaptive_discovery.enabled: false`, returns a single INFO. When
enabled, verifies:

- RULES.md contains an Authority Awareness or Adaptive Discovery section
  with at least three of the five keyword phrases.
- If `entities.enabled: true`, at least 80% of entity records have a
  non-empty `confidence_basis` field.
- The configured `high_stakes_escalation_channel` resolves: when the
  alias `authority` is used, `accountabilities.enabled` must be true and
  `authority_channel` must not be `none`. When an explicit channel slug
  is used, it must match a channel in `channels:`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

from gateway.config import (
    ADAPTIVE_DISCOVERY_AUTHORITY_ALIAS,
    SUPPORTED_CHANNELS,
    load_config,
)


KEYWORD_PHRASES = (
    "declared",
    "inferred",
    "three cautions",
    "discovery protocol",
    "mutual self-disclosure",
)
KEYWORD_MIN_HITS = 3
CONFIDENCE_BASIS_RATIO = 0.8


@dataclass(frozen=True)
class HealthItem:
    level: str
    message: str


def _rules_path(instance_dir: Path) -> Path:
    return instance_dir / "memory" / "L1" / "RULES.md"


def _entities_dir(instance_dir: Path) -> Path:
    return instance_dir / "memory" / "L2" / "entities"


def _extract_frontmatter_block(text: str) -> str | None:
    stripped = text.lstrip()
    lines = stripped.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    body: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            return "\n".join(body)
        body.append(line)
    return None


def _parse_frontmatter(text: str) -> dict | None:
    block = _extract_frontmatter_block(text)
    if block is None or yaml is None:
        return None
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:  # type: ignore[attr-defined]
        return None
    if not isinstance(data, dict):
        return None
    return data


def _check_rules_section(instance_dir: Path) -> HealthItem:
    path = _rules_path(instance_dir)
    if not path.exists():
        return HealthItem("warn", "RULES.md missing")
    try:
        text = path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError as exc:
        return HealthItem("warn", f"RULES.md unreadable: {exc}")
    has_section = (
        "authority awareness" in text or "adaptive discovery" in text
    )
    if not has_section:
        return HealthItem(
            "warn",
            "RULES.md missing the Authority Awareness / Adaptive Discovery "
            "constitutional section",
        )
    hits = sum(1 for kw in KEYWORD_PHRASES if kw in text)
    if hits < KEYWORD_MIN_HITS:
        return HealthItem(
            "warn",
            f"RULES.md adaptive-discovery section has only {hits}/"
            f"{len(KEYWORD_PHRASES)} keyword phrases "
            f"(need ≥{KEYWORD_MIN_HITS})",
        )
    return HealthItem(
        "ok",
        f"RULES.md adaptive-discovery section present "
        f"({hits}/{len(KEYWORD_PHRASES)} keywords)",
    )


def _check_confidence_basis(instance_dir: Path) -> HealthItem | None:
    """Heuristic: when entities are tracked, ≥80% need confidence_basis."""
    entities_dir = _entities_dir(instance_dir)
    if not entities_dir.exists():
        return HealthItem(
            "info",
            "entities directory absent — confidence_basis check skipped",
        )
    records = [
        p
        for p in entities_dir.glob("*.md")
        if not p.name.startswith("_")
    ]
    if not records:
        return HealthItem(
            "info",
            "no entity records — confidence_basis check skipped",
        )
    with_basis = 0
    for path in records:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        data = _parse_frontmatter(text)
        if data and (data.get("confidence_basis") or "").strip():
            with_basis += 1
    ratio = with_basis / len(records)
    if ratio < CONFIDENCE_BASIS_RATIO:
        return HealthItem(
            "warn",
            f"only {with_basis}/{len(records)} entity records have "
            f"confidence_basis ({ratio:.0%}, need "
            f"≥{int(CONFIDENCE_BASIS_RATIO * 100)}%)",
        )
    return HealthItem(
        "ok",
        f"{with_basis}/{len(records)} entity records have confidence_basis "
        f"({ratio:.0%})",
    )


def _check_escalation_channel(cfg) -> HealthItem:
    channel = cfg.adaptive_discovery.high_stakes_escalation_channel
    if channel == ADAPTIVE_DISCOVERY_AUTHORITY_ALIAS:
        if not cfg.accountabilities.enabled:
            return HealthItem(
                "warn",
                "escalation_channel `authority` requires "
                "accountabilities.enabled=true (currently false)",
            )
        if cfg.accountabilities.authority_channel == "none":
            return HealthItem(
                "warn",
                "escalation_channel `authority` resolves to "
                "accountabilities.authority_channel=`none` — no reachable "
                "escalation target",
            )
        return HealthItem(
            "ok",
            f"escalation_channel resolves to authority_channel "
            f"`{cfg.accountabilities.authority_channel}`",
        )
    if channel not in SUPPORTED_CHANNELS:
        return HealthItem(
            "warn",
            f"escalation_channel `{channel}` is not a supported channel "
            f"slug ({list(SUPPORTED_CHANNELS)})",
        )
    channel_cfg = cfg.channels.get(channel)
    if channel_cfg is None or not channel_cfg.enabled:
        return HealthItem(
            "warn",
            f"escalation_channel `{channel}` not configured in "
            f"channels: section (or not enabled)",
        )
    return HealthItem("ok", f"escalation_channel `{channel}` configured")


def check_adaptive_discovery(instance_dir: Path) -> list[HealthItem]:
    """Run all adaptive-discovery checks for `instance_dir`."""
    try:
        cfg = load_config(instance_dir)
    except Exception as exc:  # noqa: BLE001
        return [HealthItem("warn", f"gateway config unreadable: {exc}")]

    if not cfg.adaptive_discovery.enabled:
        return [HealthItem("info", "Adaptive discovery: disabled (opt-in)")]

    items: list[HealthItem] = [_check_rules_section(instance_dir)]
    if cfg.entities.enabled:
        basis_item = _check_confidence_basis(instance_dir)
        if basis_item is not None:
            items.append(basis_item)
    items.append(_check_escalation_channel(cfg))
    return items
