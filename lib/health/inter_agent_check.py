"""Inter-agent protocol health checks consumed by `jc-doctor`.

Covers docs/specs/inter-agent-protocol.md §Phase 5 — `jc-doctor`
inter-agent checks. Returns a list of `HealthItem` records the shell
doctor renders with the standard ok/warn/fail/info glyphs.

When `inter_agent_protocol.enabled: false` (default), returns a single
INFO. When enabled, verifies authority-map.md exists, parses, has the
expected frontmatter, contains an `## Agents` table whose header row
matches the expected columns, declares `self: <agent_id>` matching one
row, every `accountabilities_pointer` resolves on disk (local paths
only — cross-host pointers pass with INFO), and `RULES.md` contains an
Inter-Agent Protocol constitutional section with at least three of the
five principle keywords.
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


AUTHORITY_MAP_EXPECTED_SLUG = "authority-map"
AUTHORITY_MAP_EXPECTED_TYPE = "authority-map"
AUTHORITY_MAP_VALID_STATES = ("active", "draft", "archived")
AUTHORITY_MAP_REQUIRED_FIELDS = ("slug", "type", "state")

AGENTS_HEADER_REQUIRED = (
    "agent_id",
    "display_name",
    "role",
    "human_authority",
    "accountabilities_pointer",
    "channel",
    "instance_id",
)

PRINCIPLE_KEYWORDS = (
    "authority symmetry",
    "perimeter respect",
    "mutual respect",
    "escalation transparency",
    "authority asymmetry",
)
PRINCIPLE_MIN_HITS = 3

_SELF_LINE_RE = re.compile(r"^self:\s*([A-Za-z0-9_-]+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class HealthItem:
    level: str
    message: str


def _authority_map_path(instance_dir: Path, configured: str) -> Path:
    return instance_dir / configured


def _rules_path(instance_dir: Path) -> Path:
    return instance_dir / "memory" / "L1" / "RULES.md"


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


def _validate_frontmatter(data: dict) -> list[str]:
    errors: list[str] = []
    missing = [f for f in AUTHORITY_MAP_REQUIRED_FIELDS if not data.get(f)]
    if missing:
        errors.append(f"missing required fields: {', '.join(missing)}")
    slug = data.get("slug")
    if slug and slug != AUTHORITY_MAP_EXPECTED_SLUG:
        errors.append(
            f"slug must be `{AUTHORITY_MAP_EXPECTED_SLUG}` (got `{slug}`)"
        )
    type_ = data.get("type")
    if type_ and type_ != AUTHORITY_MAP_EXPECTED_TYPE:
        errors.append(
            f"type must be `{AUTHORITY_MAP_EXPECTED_TYPE}` (got `{type_}`)"
        )
    state = data.get("state")
    if state and state not in AUTHORITY_MAP_VALID_STATES:
        errors.append(
            f"state must be one of {list(AUTHORITY_MAP_VALID_STATES)} "
            f"(got `{state}`)"
        )
    return errors


def _parse_agents_table(text: str) -> tuple[list[str], list[dict[str, str]], str | None]:
    """Return (columns, rows, error). Rows are dicts keyed by column name.

    Skips HTML-comment-only rows and the markdown separator row.
    """
    section_re = re.compile(
        r"^##\s+Agents\s*$(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = section_re.search(text)
    if not match:
        return ([], [], "missing `## Agents` section")
    body = match.group(1)
    table_lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip().startswith("|") and line.strip().endswith("|")
    ]
    if len(table_lines) < 2:
        return ([], [], "`## Agents` table not found")
    header_cells = [c.strip() for c in table_lines[0].strip("|").split("|")]
    if not all(col in header_cells for col in AGENTS_HEADER_REQUIRED):
        missing = [c for c in AGENTS_HEADER_REQUIRED if c not in header_cells]
        return (
            header_cells,
            [],
            f"`## Agents` header missing columns: {', '.join(missing)}",
        )
    rows: list[dict[str, str]] = []
    for line in table_lines[2:]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not any(cells):
            continue
        if all(c.startswith("<!--") or not c for c in cells):
            continue
        row = {
            header_cells[i]: cells[i] if i < len(cells) else ""
            for i in range(len(header_cells))
        }
        if not row.get("agent_id"):
            continue
        rows.append(row)
    return (header_cells, rows, None)


def _check_authority_map(
    instance_dir: Path, configured_path: str, require_self: bool
) -> list[HealthItem]:
    path = _authority_map_path(instance_dir, configured_path)
    if not path.exists():
        return [
            HealthItem(
                "warn",
                f"authority-map missing: {path.relative_to(instance_dir)} "
                "(run `jc memory scaffold inter-agent`)",
            )
        ]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [HealthItem("warn", f"authority-map unreadable: {exc}")]

    items: list[HealthItem] = []
    data, fm_error = _parse_frontmatter(text)
    if fm_error is not None:
        items.append(HealthItem("warn", f"authority-map frontmatter: {fm_error}"))
        return items
    fm_errors = _validate_frontmatter(data or {})
    if fm_errors:
        for err in fm_errors:
            items.append(HealthItem("warn", f"authority-map frontmatter: {err}"))
    else:
        items.append(HealthItem("ok", "authority-map frontmatter valid"))

    _, rows, table_error = _parse_agents_table(text)
    if table_error is not None:
        items.append(HealthItem("warn", f"authority-map: {table_error}"))
        return items
    if not rows:
        items.append(HealthItem("info", "authority-map: no agent rows yet"))
    else:
        items.append(
            HealthItem("ok", f"authority-map: {len(rows)} agent row(s) parsed")
        )

    self_match = _SELF_LINE_RE.search(text)
    if not self_match:
        level = "warn" if require_self else "info"
        items.append(
            HealthItem(
                level,
                "authority-map: missing `self: <agent_id>` declaration",
            )
        )
    else:
        self_id = self_match.group(1)
        agent_ids = {r["agent_id"] for r in rows}
        if rows and self_id not in agent_ids:
            items.append(
                HealthItem(
                    "warn",
                    f"authority-map: self=`{self_id}` does not match any "
                    "row in the Agents table",
                )
            )
        else:
            items.append(
                HealthItem("ok", f"authority-map: self=`{self_id}` declared")
            )

    # Validate accountabilities_pointer reachability for local paths only.
    for row in rows:
        pointer = (row.get("accountabilities_pointer") or "").strip()
        if not pointer or pointer == "TBD":
            continue
        if pointer.startswith("/") or pointer.startswith(".."):
            items.append(
                HealthItem(
                    "info",
                    f"authority-map: {row['agent_id']} pointer `{pointer}` "
                    "is cross-instance (not validated)",
                )
            )
            continue
        target = (instance_dir / pointer).resolve()
        if not target.exists():
            items.append(
                HealthItem(
                    "warn",
                    f"authority-map: {row['agent_id']} pointer `{pointer}` "
                    "does not resolve on disk",
                )
            )
        else:
            items.append(
                HealthItem(
                    "ok",
                    f"authority-map: {row['agent_id']} pointer resolved",
                )
            )

    return items


def _check_rules_section(instance_dir: Path) -> HealthItem:
    path = _rules_path(instance_dir)
    if not path.exists():
        return HealthItem("warn", "RULES.md missing")
    try:
        text = path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError as exc:
        return HealthItem("warn", f"RULES.md unreadable: {exc}")
    if "inter-agent protocol" not in text:
        return HealthItem(
            "warn",
            "RULES.md missing the Inter-Agent Protocol constitutional section",
        )
    hits = sum(1 for kw in PRINCIPLE_KEYWORDS if kw in text)
    if hits < PRINCIPLE_MIN_HITS:
        return HealthItem(
            "warn",
            f"RULES.md Inter-Agent Protocol section has only {hits}/"
            f"{len(PRINCIPLE_KEYWORDS)} principle keywords "
            f"(need ≥{PRINCIPLE_MIN_HITS})",
        )
    return HealthItem(
        "ok",
        f"RULES.md Inter-Agent Protocol section present ({hits}/"
        f"{len(PRINCIPLE_KEYWORDS)} principles)",
    )


def check_inter_agent(instance_dir: Path) -> list[HealthItem]:
    """Run all inter-agent protocol checks for `instance_dir`."""
    try:
        cfg = load_config(instance_dir)
    except Exception as exc:  # noqa: BLE001
        return [HealthItem("warn", f"gateway config unreadable: {exc}")]

    iap = cfg.inter_agent_protocol
    if not iap.enabled:
        return [HealthItem("info", "Inter-agent protocol: disabled (opt-in)")]

    items = _check_authority_map(
        instance_dir, iap.authority_map_path, iap.require_self_declaration
    )
    items.append(_check_rules_section(instance_dir))
    return items
