"""Section-aware splice — replace `{{slot:<id>}}` placeholders with composed body.

Atomic write with backup. Backups land in `state/persona/redo/<timestamp>/
<slot_id>.bak` so brownfield overwrites are recoverable.

The splice keeps the surrounding markdown intact:
  - The H2 heading line is preserved.
  - Any `<!-- IMMUTABILE | REVIEWABLE | OPEN -->` marker comment is preserved.
  - Any `<!-- ASK: ... -->` comment is removed (it was scaffolding for the
    interview; once filled, it's noise in the rendered file).
  - The `{{slot:<id>}}` placeholder is replaced by the composed body.

If the slot's section is missing entirely (the file exists but the heading
isn't present), splice() can optionally insert it at the end of the file
when `create_if_missing=True`.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .questions import Slot


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_ASK_COMMENT_RE = re.compile(r"<!--\s*ASK:.*?-->\s*\n?", re.DOTALL)
_SECTION_HEADING_RE = re.compile(r"^## .+$", re.MULTILINE)


class SpliceError(RuntimeError):
    """Raised when splice cannot proceed safely."""


def splice_slot_body(
    instance_dir: Path,
    slot: Slot,
    composed_body: str,
    *,
    backup_root: Path | None = None,
    create_if_missing: bool = True,
) -> None:
    """Replace the slot's placeholder with `composed_body` in the target file.

    - `instance_dir` — instance root.
    - `slot` — the Slot definition.
    - `composed_body` — the markdown to put in place of the `{{slot:<id>}}`
      placeholder. Should NOT include the section heading.
    - `backup_root` — directory under `instance_dir/state/persona/redo/` to
      write the prior file content to. Defaults to a timestamped dir.
    - `create_if_missing` — if True and the section is absent, append a new
      section at the end of the file (with marker + composed body).
    """
    target_path = instance_dir / slot.target_file
    if not target_path.exists():
        raise SpliceError(f"target file not found: {slot.target_file}")

    text = target_path.read_text(encoding="utf-8")
    new_text = _replace_section_body(text, slot, composed_body, create_if_missing)

    if new_text == text:
        return  # no change

    _backup_file(instance_dir, slot, target_path, text, backup_root)
    _atomic_write(target_path, new_text)


def _replace_section_body(
    text: str,
    slot: Slot,
    composed_body: str,
    create_if_missing: bool,
) -> str:
    """Return new file text with the section's body replaced."""
    heading_pattern = re.compile(
        r"^" + re.escape(slot.target_section) + r"\s*\n",
        re.MULTILINE,
    )
    match = heading_pattern.search(text)
    if not match:
        if not create_if_missing:
            raise SpliceError(
                f"section not found in {slot.target_file}: {slot.target_section!r}"
            )
        return _append_new_section(text, slot, composed_body)

    body_start = match.end()
    next_heading = _SECTION_HEADING_RE.search(text, pos=body_start)
    body_end = next_heading.start() if next_heading else len(text)
    old_body = text[body_start:body_end]

    new_body = _build_new_body(old_body, slot, composed_body)
    return text[:body_start] + new_body + text[body_end:]


def _build_new_body(old_body: str, slot: Slot, composed_body: str) -> str:
    """Construct the new section body.

    Strategy:
      - Preserve the IMMUTABILE/REVIEWABLE/OPEN marker line if present.
      - Drop ASK comments (they were prompts; the answer is in composed_body).
      - Drop the {{slot:...}} placeholder line.
      - Insert composed_body in its place.
    """
    marker = _extract_marker(old_body)

    composed = composed_body.rstrip()
    if not composed:
        composed = ""

    parts: list[str] = []
    if marker:
        parts.append(marker)
        parts.append("")
    if composed:
        parts.append(composed)
    parts.append("")
    parts.append("")
    return "\n".join(parts)


def _extract_marker(body: str) -> str:
    """Return the first IMMUTABILE/REVIEWABLE/OPEN marker line in the body, or ''."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped in ("<!-- IMMUTABILE -->", "<!-- REVIEWABLE -->", "<!-- OPEN -->"):
            return stripped
    return ""


def _append_new_section(text: str, slot: Slot, composed_body: str) -> str:
    """Append a new section to the file."""
    suffix = "\n" if not text.endswith("\n") else ""
    block = (
        f"\n{slot.target_section}\n"
        f"<!-- REVIEWABLE -->\n\n"
        f"{composed_body.rstrip()}\n"
    )
    return text + suffix + block


# ---------------------------------------------------------------------------
# Backups + atomic writes
# ---------------------------------------------------------------------------

def _backup_file(
    instance_dir: Path,
    slot: Slot,
    target_path: Path,
    prior_content: str,
    backup_root: Path | None,
) -> None:
    if backup_root is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_root = instance_dir / "state" / "persona" / "redo" / ts
    backup_root.mkdir(parents=True, exist_ok=True)
    safe_slot_name = slot.slot_id.replace("/", "_")
    backup_path = backup_root / f"{safe_slot_name}__{target_path.name}.bak"
    backup_path.write_text(prior_content, encoding="utf-8")


def _atomic_write(path: Path, content: str) -> None:
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
