"""Apply proposals to memory files with atomic writes + backups + DKIM gate."""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import frozen_sections
from .store import Proposal


class ApplierError(RuntimeError):
    """Raised when a proposal cannot be applied safely."""
    pass


def apply_proposal(instance_dir: Path, proposal: Proposal) -> None:
    """Apply a proposal to memory files atomically. Backup prior version.

    Gate order:
      1. Path security (no escape from memory/).
      2. Frozen-section list (regex match on target_section).
      3. HTML marker scan (<!-- IMMUTABILE --> immediately under heading).
      4. DKIM verification (skipped only for JOURNAL.md append).
    """
    target_path = instance_dir / proposal.target_file
    if not target_path.exists():
        raise ApplierError(f"target file not found: {proposal.target_file}")

    # Security: reject paths outside memory/
    if not str(target_path.resolve()).startswith(str((instance_dir / "memory").resolve())):
        raise ApplierError(f"path escape attempt: {proposal.target_file}")

    # JOURNAL.md is auto-apply scope (append-only) — skip DKIM/frozen checks for append.
    is_journal = proposal.target_file == "memory/L1/JOURNAL.md"

    if not is_journal:
        if not _verify_dkim_approval(instance_dir, proposal.id):
            raise ApplierError(
                "DKIM email approval required for RULES/IDENTITY changes; not found"
            )
        if frozen_sections.is_section_frozen(proposal.target_file, proposal.target_section):
            raise ApplierError(
                f"section IMMUTABILE — only Filippo via DKIM email can modify: "
                f"{proposal.target_section}"
            )
        if _section_marker_immutable(target_path, proposal.target_section):
            raise ApplierError(
                f"section marked <!-- IMMUTABILE --> in file: {proposal.target_section}"
            )

    if proposal.type == "modify":
        _apply_modify(instance_dir, target_path, proposal)
    elif proposal.type == "add":
        _apply_add(instance_dir, target_path, proposal)
    elif proposal.type == "remove":
        _apply_remove(instance_dir, target_path, proposal)
    else:
        raise ApplierError(f"unknown proposal type: {proposal.type}")


def _verify_dkim_approval(instance_dir: Path, proposal_id: str) -> bool:
    """Check inbox for a DKIM-signed Filippo email approving this proposal_id.

    TODO: read inbox via `ops/email-check.py --json`, look for a Message-ID with body
    referencing `proposal_id`, verify DKIM-signed by `filippo.perta@scovai.com`. The
    approval marker should be a terse confirmation ("OK enact" or equivalent) tied to
    the proposal id. Until implemented, this returns False and only JOURNAL.md auto-
    apply works.
    """
    return False


def _section_marker_immutable(target_path: Path, target_section: str | None) -> bool:
    """Return True if heading is followed (within first 3 non-empty lines) by IMMUTABILE marker."""
    if not target_section:
        return False
    try:
        content = target_path.read_text(encoding="utf-8")
    except OSError:
        return False

    # Find the heading line.
    pattern = re.escape(target_section)
    match = re.search(pattern, content)
    if not match:
        return False

    # Read up to 3 non-empty lines after the heading.
    after = content[match.end():].splitlines()
    seen = 0
    for line in after:
        stripped = line.strip()
        if not stripped:
            continue
        if frozen_sections.MARKER_IMMUTABILE in stripped:
            return True
        seen += 1
        if seen >= 3:
            break
    return False


def _backup_file(instance_dir: Path, target_path: Path) -> None:
    """Create timestamped backup in memory/.history/"""
    backup_dir = instance_dir / "memory" / ".history"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "").replace(":", "").replace("-", "")
    backup_path = backup_dir / f"{target_path.name}.{ts}"
    backup_path.write_bytes(target_path.read_bytes())


def _apply_modify(instance_dir: Path, target_path: Path, proposal: Proposal) -> None:
    """Modify a section of the file."""
    content = target_path.read_text(encoding="utf-8")

    if proposal.target_section:
        pattern = rf"({re.escape(proposal.target_section)}.*?)(?=\n##|\Z)"
        match = re.search(pattern, content, re.DOTALL)
        if not match:
            raise ApplierError(f"section not found: {proposal.target_section}")

        section_content = match.group(1)
        if proposal.current_content not in section_content:
            raise ApplierError(f"current_content not found in section {proposal.target_section}")

        new_section = section_content.replace(proposal.current_content, proposal.proposed_content)
        new_content = content[: match.start(1)] + new_section + content[match.end(1) :]
    else:
        if proposal.current_content not in content:
            raise ApplierError("current_content not found (no section specified)")
        new_content = content.replace(proposal.current_content, proposal.proposed_content)

    _backup_file(instance_dir, target_path)
    _atomic_write(target_path, new_content)


def _apply_add(instance_dir: Path, target_path: Path, proposal: Proposal) -> None:
    """Add content to a section."""
    content = target_path.read_text(encoding="utf-8")

    if proposal.target_section:
        pattern = rf"({re.escape(proposal.target_section)}.*?)(?=\n##|\Z)"
        match = re.search(pattern, content, re.DOTALL)
        if not match:
            raise ApplierError(f"section not found: {proposal.target_section}")

        insertion_point = match.end(1)
        new_content = content[:insertion_point] + "\n" + proposal.proposed_content + content[insertion_point:]
    else:
        new_content = content + "\n" + proposal.proposed_content

    _backup_file(instance_dir, target_path)
    _atomic_write(target_path, new_content)


def _apply_remove(instance_dir: Path, target_path: Path, proposal: Proposal) -> None:
    """Remove content from the file."""
    content = target_path.read_text(encoding="utf-8")

    if proposal.current_content not in content:
        raise ApplierError("current_content not found (cannot remove)")

    new_content = content.replace(proposal.current_content, "")
    _backup_file(instance_dir, target_path)
    _atomic_write(target_path, new_content)


def _atomic_write(path: Path, content: str) -> None:
    """Write to temp file then rename (atomic on POSIX)."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        delete=False,
        encoding="utf-8",
        suffix=".tmp",
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
