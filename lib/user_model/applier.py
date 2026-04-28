"""Apply proposals to memory files with atomic writes + backups."""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .store import Proposal


class ApplierError(RuntimeError):
    """Raised when a proposal cannot be applied safely."""
    pass


def apply_proposal(instance_dir: Path, proposal: Proposal) -> None:
    """Apply a proposal to memory files atomically. Backup prior version."""
    target_path = instance_dir / proposal.target_file
    if not target_path.exists():
        raise ApplierError(f"target file not found: {proposal.target_file}")

    # Security: reject paths outside memory/
    if not str(target_path.resolve()).startswith(str((instance_dir / "memory").resolve())):
        raise ApplierError(f"path escape attempt: {proposal.target_file}")

    if proposal.type == "modify":
        _apply_modify(instance_dir, target_path, proposal)
    elif proposal.type == "add":
        _apply_add(instance_dir, target_path, proposal)
    elif proposal.type == "remove":
        _apply_remove(instance_dir, target_path, proposal)
    else:
        raise ApplierError(f"unknown proposal type: {proposal.type}")


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

    # Find section by heading if specified.
    if proposal.target_section:
        # Build regex to find the section and match current_content.
        section_pattern = re.escape(proposal.target_section)
        # Match from heading to next heading or EOF.
        pattern = f"({re.escape(proposal.target_section)}.*?)(?=\n##|\Z)"
        match = re.search(pattern, content, re.DOTALL)
        if not match:
            raise ApplierError(f"section not found: {proposal.target_section}")

        section_content = match.group(1)
        if proposal.current_content not in section_content:
            raise ApplierError(f"current_content not found in section {proposal.target_section}")

        new_section = section_content.replace(proposal.current_content, proposal.proposed_content)
        new_content = content[: match.start(1)] + new_section + content[match.end(1) :]
    else:
        # Top-level modify (exact match required).
        if proposal.current_content not in content:
            raise ApplierError("current_content not found (no section specified)")
        new_content = content.replace(proposal.current_content, proposal.proposed_content)

    _backup_file(instance_dir, target_path)
    _atomic_write(target_path, new_content)


def _apply_add(instance_dir: Path, target_path: Path, proposal: Proposal) -> None:
    """Add content to a section."""
    content = target_path.read_text(encoding="utf-8")

    if proposal.target_section:
        # Find section and append to it.
        pattern = f"({re.escape(proposal.target_section)}.*?)(?=\n##|\Z)"
        match = re.search(pattern, content, re.DOTALL)
        if not match:
            raise ApplierError(f"section not found: {proposal.target_section}")

        insertion_point = match.end(1)
        # Append with newline separator.
        new_content = content[:insertion_point] + "\n" + proposal.proposed_content + content[insertion_point:]
    else:
        # Append to end of file.
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
        # Cleanup temp file if rename fails.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
