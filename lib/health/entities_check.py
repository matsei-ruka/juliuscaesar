"""Entities health checks consumed by `jc-doctor`.

Covers docs/specs/relational-awareness-layer.md §Phase 5 — `jc-doctor`
entity checks. The function returns a list of `HealthItem` records that
the shell-side doctor renders with the standard ok/warn/fail/info glyphs.

When `entities.enabled: false` (default), returns a single INFO item so
operators see the feature exists and is opt-in. When enabled, runs the
spec's heuristic checks: directory exists, at least one record present,
each `.md` file parses with valid frontmatter, required fields present
with valid enum values, slug stability.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

from gateway.config import load_config


ENTITY_CATEGORIES = (
    "internal_authority",
    "internal_peer",
    "external_client",
    "external_vendor",
    "external_occasional",
    "unknown",
)
KNOWLEDGE_STATES = ("declared", "inferred", "hybrid")
CONFIDENCE_LEVELS = ("high", "medium", "low")
REQUIRED_FIELDS = (
    "slug",
    "entity_category",
    "knowledge_state",
    "classification_confidence",
)


@dataclass(frozen=True)
class HealthItem:
    level: str  # "ok" | "warn" | "error" | "info"
    message: str


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


def _parse_frontmatter(text: str) -> tuple[dict | None, str | None]:
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


def _validate_entity_frontmatter(data: dict, filename: str) -> list[str]:
    errors: list[str] = []
    missing = [f for f in REQUIRED_FIELDS if not data.get(f)]
    if missing:
        errors.append(f"missing required fields: {', '.join(missing)}")
    category = data.get("entity_category")
    if category and category not in ENTITY_CATEGORIES:
        errors.append(
            f"entity_category `{category}` not in closed enum "
            f"{list(ENTITY_CATEGORIES)}"
        )
    state = data.get("knowledge_state")
    if state and state not in KNOWLEDGE_STATES:
        errors.append(
            f"knowledge_state `{state}` must be one of {list(KNOWLEDGE_STATES)}"
        )
    confidence = data.get("classification_confidence")
    if confidence and confidence not in CONFIDENCE_LEVELS:
        errors.append(
            f"classification_confidence `{confidence}` must be one of "
            f"{list(CONFIDENCE_LEVELS)}"
        )
    slug = data.get("slug")
    stem = Path(filename).stem
    if slug and slug != stem:
        errors.append(f"slug `{slug}` does not match filename stem `{stem}`")
    return errors


def _check_entity_files(entities_dir: Path) -> list[HealthItem]:
    records = sorted(
        p
        for p in entities_dir.glob("*.md")
        if not p.name.startswith("_")
    )
    if not records:
        return [HealthItem("info", "no entities recorded yet")]
    items: list[HealthItem] = []
    for path in records:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            items.append(
                HealthItem("warn", f"{path.name}: unreadable ({exc})")
            )
            continue
        data, error = _parse_frontmatter(text)
        if error is not None:
            items.append(HealthItem("warn", f"{path.name}: {error}"))
            continue
        validation_errors = _validate_entity_frontmatter(data or {}, path.name)
        if validation_errors:
            for err in validation_errors:
                items.append(HealthItem("warn", f"{path.name}: {err}"))
        else:
            items.append(HealthItem("ok", f"{path.name}: frontmatter valid"))
    return items


def check_entities(instance_dir: Path) -> list[HealthItem]:
    """Run all entity checks for `instance_dir`.

    Returns a single INFO item when the feature is disabled. When enabled,
    checks the directory exists, lists records, and validates frontmatter
    schema on each record.
    """
    try:
        cfg = load_config(instance_dir)
    except Exception as exc:  # noqa: BLE001
        return [HealthItem("warn", f"gateway config unreadable: {exc}")]

    if not cfg.entities.enabled:
        return [HealthItem("info", "Entities: disabled (opt-in)")]

    entities_dir = _entities_dir(instance_dir)
    if not entities_dir.exists():
        return [
            HealthItem(
                "warn",
                f"entities directory missing: "
                f"{entities_dir.relative_to(instance_dir)} "
                "(run `jc memory scaffold entities`)",
            )
        ]
    items: list[HealthItem] = [
        HealthItem("ok", f"entities directory present: memory/L2/entities/")
    ]
    items.extend(_check_entity_files(entities_dir))
    return items
