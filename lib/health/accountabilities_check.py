"""Accountabilities health checks consumed by `jc-doctor`.

Covers docs/specs/accountabilities.md §Phase 5 — `jc-doctor` accountability
checks. The function returns a list of `HealthItem` records that the
shell-side doctor renders with the standard ok/warn/fail/info glyphs.

When `accountabilities.enabled: false` (default), returns a single INFO
item so operators see the feature exists and is opt-in. When enabled,
runs the spec's four heuristic checks (manifest, L2 details, RULES
section, audit log) and emits an item per check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

from gateway.config import load_config


REQUIRED_DETAIL_SECTIONS = (
    "Scope",
    "Out of scope",
    "Outputs",
    "Stakeholders",
    "Cadence",
    "Decision boundary",
    "Adjacency notes",
    "Self-check pre-action",
    "Connections to existing constitution",
)

ENGAGEMENT_LEVEL_NAMES = ("Inside", "Adjacent", "Outside", "Delegated")

MANIFEST_REQUIRED_FIELDS = ("slug", "title", "layer", "type", "state", "version")
MANIFEST_VALID_STATES = ("active", "draft", "archived")
MANIFEST_EXPECTED_SLUG = "accountabilities-manifest"
MANIFEST_EXPECTED_LAYER = "L1"
MANIFEST_EXPECTED_TYPE = "manifest"

_MANIFEST_DETAIL_LINK = re.compile(
    r"\[[^\]]+\]\(\.\.\/L2\/accountabilities\/([A-Za-z0-9_-]+)\.md\)"
)


@dataclass(frozen=True)
class HealthItem:
    level: str  # "ok" | "warn" | "error" | "info"
    message: str


def _manifest_path(instance_dir: Path) -> Path:
    return instance_dir / "memory" / "L1" / "accountabilities-manifest.md"


def _l2_dir(instance_dir: Path) -> Path:
    return instance_dir / "memory" / "L2" / "accountabilities"


def _rules_path(instance_dir: Path) -> Path:
    return instance_dir / "memory" / "L1" / "RULES.md"


def _audit_path(instance_dir: Path) -> Path:
    return instance_dir / "memory" / "L2" / "accountabilities" / "_audit.md"


def _extract_frontmatter_block(text: str) -> str | None:
    """Return the YAML body between leading `---` delimiters, or None."""
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


def _parse_frontmatter(text: str) -> tuple[dict | None, str | None]:
    """Return (parsed_dict, error_message). Both may be None."""
    block = _extract_frontmatter_block(text)
    if block is None:
        return (None, "no YAML frontmatter delimiters")
    if yaml is None:
        return (None, "PyYAML not installed")
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:  # type: ignore[attr-defined]
        return (None, f"malformed YAML frontmatter: {exc}")
    if data is None:
        return ({}, None)
    if not isinstance(data, dict):
        return (None, "frontmatter is not a YAML mapping")
    return (data, None)


def _validate_manifest_frontmatter(data: dict) -> list[str]:
    """Return a list of validation error strings. Empty list means valid."""
    errors: list[str] = []
    missing = [f for f in MANIFEST_REQUIRED_FIELDS if not data.get(f)]
    if missing:
        errors.append(f"missing required fields: {', '.join(missing)}")
    slug = data.get("slug")
    if slug and slug != MANIFEST_EXPECTED_SLUG:
        errors.append(
            f"slug must be `{MANIFEST_EXPECTED_SLUG}` (got `{slug}`)"
        )
    layer = data.get("layer")
    if layer and layer != MANIFEST_EXPECTED_LAYER:
        errors.append(
            f"layer must be `{MANIFEST_EXPECTED_LAYER}` (got `{layer}`)"
        )
    type_ = data.get("type")
    if type_ and type_ != MANIFEST_EXPECTED_TYPE:
        errors.append(
            f"type must be `{MANIFEST_EXPECTED_TYPE}` (got `{type_}`)"
        )
    state = data.get("state")
    if state and state not in MANIFEST_VALID_STATES:
        errors.append(
            f"state must be one of {list(MANIFEST_VALID_STATES)} (got `{state}`)"
        )
    return errors


def _referenced_detail_slugs(manifest_text: str) -> list[str]:
    return _MANIFEST_DETAIL_LINK.findall(manifest_text)


def _detail_has_all_sections(text: str) -> tuple[bool, list[str]]:
    missing = [s for s in REQUIRED_DETAIL_SECTIONS if s not in text]
    return (not missing, missing)


def _rules_has_constitutional_section(text: str) -> bool:
    if "Accountability Principle" not in text:
        return False
    matches = sum(1 for name in ENGAGEMENT_LEVEL_NAMES if name in text)
    return matches >= 2


def _check_manifest(instance_dir: Path) -> tuple[HealthItem, list[str]]:
    path = _manifest_path(instance_dir)
    if not path.exists():
        return (
            HealthItem("warn", f"manifest missing: {path.relative_to(instance_dir)}"),
            [],
        )
    text = path.read_text(encoding="utf-8")
    data, error = _parse_frontmatter(text)
    if error is not None:
        return (
            HealthItem(
                "warn",
                f"manifest frontmatter unreadable ({path.relative_to(instance_dir)}): {error}",
            ),
            [],
        )
    assert data is not None  # narrowing: error is None implies data is not None
    validation_errors = _validate_manifest_frontmatter(data)
    if validation_errors:
        return (
            HealthItem(
                "warn",
                f"manifest frontmatter invalid: {'; '.join(validation_errors)}",
            ),
            _referenced_detail_slugs(text),
        )
    return (
        HealthItem("ok", "manifest present and parseable"),
        _referenced_detail_slugs(text),
    )


def _check_detail_files(instance_dir: Path, slugs: list[str]) -> list[HealthItem]:
    if not slugs:
        return [HealthItem("info", "manifest lists no accountabilities yet")]
    items: list[HealthItem] = []
    l2 = _l2_dir(instance_dir)
    for slug in slugs:
        detail = l2 / f"{slug}.md"
        if not detail.exists():
            items.append(HealthItem("warn", f"detail file missing: {detail.relative_to(instance_dir)}"))
            continue
        ok, missing = _detail_has_all_sections(detail.read_text(encoding="utf-8"))
        if not ok:
            items.append(
                HealthItem(
                    "warn",
                    f"detail {slug}: missing sections {', '.join(missing)}",
                )
            )
        else:
            items.append(HealthItem("ok", f"detail {slug}: all 9 sections present"))
    return items


def _check_rules(instance_dir: Path) -> HealthItem:
    path = _rules_path(instance_dir)
    if not path.exists():
        return HealthItem("warn", f"RULES.md missing: {path.relative_to(instance_dir)}")
    if not _rules_has_constitutional_section(path.read_text(encoding="utf-8")):
        return HealthItem(
            "warn",
            "RULES.md lacks the §-numbered Accountability Principle section "
            "(paste from templates/instance/memory/L1/RULES.md.accountability-section.template)",
        )
    return HealthItem("ok", "RULES.md contains the Accountability Principle section")


def _check_audit(instance_dir: Path) -> HealthItem:
    path = _audit_path(instance_dir)
    if not path.exists():
        return HealthItem(
            "warn",
            f"audit log missing: {path.relative_to(instance_dir)} "
            "(will be created on first enactment)",
        )
    return HealthItem("ok", "audit log present")


def check_accountabilities(instance_dir: Path) -> list[HealthItem]:
    """Run all accountability checks for `instance_dir`.

    Returns a single INFO item when the feature is disabled. When enabled,
    returns one item per check (manifest, detail files, RULES.md, audit log).
    """
    try:
        cfg = load_config(instance_dir)
    except Exception as exc:  # noqa: BLE001 — surface as warn, not crash
        return [HealthItem("warn", f"gateway config unreadable: {exc}")]

    if not cfg.accountabilities.enabled:
        return [HealthItem("info", "Accountabilities: disabled (opt-in)")]

    items: list[HealthItem] = []
    manifest_item, slugs = _check_manifest(instance_dir)
    items.append(manifest_item)
    if manifest_item.level == "ok":
        items.extend(_check_detail_files(instance_dir, slugs))
    items.append(_check_rules(instance_dir))
    items.append(_check_audit(instance_dir))
    return items
