"""Append-only audit log for accountability manifest enactments.

Covers docs/specs/accountabilities.md §Phase 4 — Audit trail writer.

The agent calls `append_audit_entry` after a primary-operator enactment
(see spec §"Authority for manifest changes"). On first call the audit
file is created with frontmatter + table header; subsequent calls
append a single Markdown table row, preserving order.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


AUDIT_RELATIVE_PATH = Path("memory") / "L2" / "accountabilities" / "_audit.md"


_AUDIT_HEADER = """---
slug: accountabilities-audit
type: audit-log
state: active
---

# Accountabilities audit log

| Timestamp | Change | Source (chat_id, message_id) | Token observed |
|---|---|---|---|
"""


@dataclass(frozen=True)
class AuditEntry:
    timestamp: str
    change: str
    source_chat_id: str
    source_message_id: str
    token_observed: str


def audit_path(instance_dir: Path) -> Path:
    return instance_dir / AUDIT_RELATIVE_PATH


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _format_row(entry: AuditEntry) -> str:
    source = f"{_escape_cell(entry.source_chat_id)}, {_escape_cell(entry.source_message_id)}"
    return (
        f"| {_escape_cell(entry.timestamp)}"
        f" | {_escape_cell(entry.change)}"
        f" | {source}"
        f" | {_escape_cell(entry.token_observed)} |\n"
    )


def append_audit_entry(instance_dir: Path, entry: AuditEntry) -> None:
    """Append `entry` to the instance's accountabilities audit log.

    Creates the file with frontmatter + table header on first call.
    Idempotent in creation; each call appends exactly one row.
    """
    target = audit_path(instance_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(_AUDIT_HEADER, encoding="utf-8")
    with target.open("a", encoding="utf-8") as fh:
        fh.write(_format_row(entry))
