"""Consolidation phase: detect signals and memory hygiene issues."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from self_model.conf import DetectorsConfig, SelfModelConfig
from self_model.corpus import iter_assistant_messages, iter_user_messages
from self_model.detector import Signal, detect_all

from .schema import BrokenLink, ConsolidationFindings, DuplicateGroup, Reflection, StaleEntry


WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")


def consolidate(instance_dir: Path, reflection: Reflection) -> ConsolidationFindings:
    events = list(iter_assistant_messages(instance_dir, 30)) + list(iter_user_messages(instance_dir, 30))
    rules = _read(instance_dir / "memory" / "L1" / "RULES.md")
    cfg = SelfModelConfig(
        enabled=True,
        mode="dry_run",
        detectors=DetectorsConfig(
            filippo_correction=True,
            hot_flag=True,
            direct_request=True,
            episode_flag=True,
            scan_weekly=False,
        ),
    )
    signals = list(detect_all(instance_dir, events, cfg, rules))
    return ConsolidationFindings(
        signals=signals,
        duplicates=_duplicates(instance_dir),
        contradictions=[],
        broken_backlinks=_broken_backlinks(instance_dir),
        stale_timestamps=_stale_entries(instance_dir, today=reflection.window_end.date()),
    )


def _duplicates(instance_dir: Path) -> list[DuplicateGroup]:
    groups: dict[str, list[str]] = defaultdict(list)
    root = instance_dir / "memory" / "L2"
    if not root.exists():
        return []
    for path in root.rglob("*.md"):
        title = _frontmatter_value(path, "title") or path.stem
        tags = _frontmatter_value(path, "tags") or ""
        body = _read(path)
        key = f"{title.lower()}|{tags.lower()}|{body[:200].strip().lower()}"
        groups[key].append(str(path.relative_to(instance_dir)))
    return [
        DuplicateGroup(paths=tuple(paths), reason="same title/tags/content prefix")
        for paths in groups.values()
        if len(paths) > 1
    ]


def _broken_backlinks(instance_dir: Path) -> list[BrokenLink]:
    index = _slug_index(instance_dir)
    out: list[BrokenLink] = []
    root = instance_dir / "memory"
    if not root.exists():
        return out
    for path in root.rglob("*.md"):
        text = _read(path)
        for match in WIKILINK_RE.finditer(text):
            target = match.group(1).strip()
            if target and target not in index:
                start = max(0, match.start() - 80)
                end = min(len(text), match.end() + 80)
                out.append(
                    BrokenLink(
                        source=str(path.relative_to(instance_dir)),
                        target=target,
                        context=text[start:end].replace("\n", " "),
                    )
                )
    return out


def _stale_entries(instance_dir: Path, *, today: date) -> list[StaleEntry]:
    out: list[StaleEntry] = []
    root = instance_dir / "memory" / "L2"
    if not root.exists():
        return out
    cutoff = today - timedelta(days=90)
    for path in root.rglob("*.md"):
        last_verified = _frontmatter_value(path, "last_verified")
        if not last_verified or last_verified in {'""', "''"}:
            continue
        try:
            verified = date.fromisoformat(last_verified.strip('"'))
        except ValueError:
            continue
        if verified < cutoff and _looks_live_world(_read(path)):
            out.append(
                StaleEntry(
                    path=str(path.relative_to(instance_dir)),
                    last_verified=last_verified,
                    reason="older than 90 days and references live-world state",
                )
            )
    return out


def _slug_index(instance_dir: Path) -> set[str]:
    slugs: set[str] = set()
    root = instance_dir / "memory"
    if not root.exists():
        return slugs
    for path in root.rglob("*.md"):
        slugs.add(path.stem)
        slug = _frontmatter_value(path, "slug")
        if slug:
            slugs.add(slug)
            slugs.add(slug.split("/")[-1])
    return slugs


def _frontmatter_value(path: Path, key: str) -> str | None:
    for line in _read(path).splitlines()[1:80]:
        if line.strip() == "---":
            break
        if line.startswith(f"{key}:"):
            return line.partition(":")[2].strip()
    return None


def _looks_live_world(text: str) -> bool:
    return bool(re.search(r"\b20\d{2}\b|state:\s*active|price|CEO|president", text, re.I))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
