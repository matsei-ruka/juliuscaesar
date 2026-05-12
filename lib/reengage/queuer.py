"""Translate silence states into commitment YAML files."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from commitments.engine import add_commitment, archived_with_tag, cancel_by_tag, pending_with_tag
from commitments.schema import Commitment, CommitmentError

from .conf import ReengageConfig, TrackedChat, load_config, zoneinfo
from .detector import SilenceState, detect


@dataclass
class ReengageSummary:
    ok: bool = True
    skipped: list[str] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    canceled: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "queued": self.queued,
            "canceled": self.canceled,
            "errors": self.errors,
            "dry_run": self.dry_run,
        }


def run(
    instance_dir: Path,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    cfg: ReengageConfig | None = None,
) -> ReengageSummary:
    cfg = cfg or load_config(instance_dir)
    summary = ReengageSummary(dry_run=dry_run)
    if not cfg.enabled:
        summary.skipped.append("disabled")
        return summary
    current = now or datetime.now(timezone.utc)
    for chat in cfg.tracked_chats:
        state = detect(instance_dir, chat, now=current)
        _process_chat(instance_dir, cfg, state, current=current, dry_run=dry_run, summary=summary)
    return summary


def cancel_if_tracked(instance_dir: Path, chat_id: int | str, *, dry_run: bool = False) -> list[str]:
    cfg = load_config(instance_dir)
    if not cfg.enabled or cfg.chat(chat_id) is None:
        return []
    return cancel_by_tag(instance_dir, _chat_tag(chat_id), dry_run=dry_run)


def _process_chat(
    instance_dir: Path,
    cfg: ReengageConfig,
    state: SilenceState,
    *,
    current: datetime,
    dry_run: bool,
    summary: ReengageSummary,
) -> None:
    chat_label = str(state.chat.chat_id)
    tag = _chat_tag(state.chat.chat_id)
    if not state.has_inbound:
        summary.skipped.append(f"{chat_label}:no-inbound")
        return
    if state.silence_hours is not None and state.silence_hours < cfg.silence_threshold_hours:
        canceled = cancel_by_tag(instance_dir, tag, dry_run=dry_run)
        summary.canceled.extend(canceled)
        summary.skipped.append(f"{chat_label}:active")
        return
    pending = pending_with_tag(instance_dir, tag)
    if pending:
        summary.skipped.append(f"{chat_label}:already-pending")
        return
    touch_n = _next_touch(instance_dir, tag, since=state.last_user_ts)
    if touch_n > cfg.max_touches or touch_n > len(cfg.touch_schedule):
        summary.skipped.append(f"{chat_label}:max-touches")
        return
    touch_tag = f"touch:{touch_n}"
    template = state.chat.templates.get(f"touch_{touch_n}")
    if not template:
        summary.ok = False
        summary.errors.append(f"{chat_label}: missing template touch_{touch_n}")
        return
    template_path = instance_dir / "memory" / "L2" / template
    if not template_path.exists():
        summary.ok = False
        summary.errors.append(f"{chat_label}: template not found: {template}")
        return
    text = template_path.read_text(encoding="utf-8").strip()
    if not text:
        summary.ok = False
        summary.errors.append(f"{chat_label}: template empty: {template}")
        return
    due = _target_due(cfg, state.chat, state.last_user_ts, cfg.touch_schedule[touch_n - 1])
    commitment = Commitment(
        slug=_slug(state.chat.chat_id, touch_n, due),
        created_at=_as_utc(current),
        due_at=due,
        action="telegram-send",
        chat_id=state.chat.chat_id,
        text=text,
        tags=("re-engagement", tag, touch_tag),
        repeat=None,
        origin="reengage",
        metadata={
            "retries": 0,
            "last_user_ts": _as_utc(state.last_user_ts).isoformat(timespec="seconds"),
            "silence_hours": round(state.silence_hours or 0, 2),
            "template": template,
        },
    )
    if dry_run:
        summary.queued.append(commitment.slug)
        return
    try:
        add_commitment(instance_dir, commitment)
    except CommitmentError as exc:
        summary.ok = False
        summary.errors.append(f"{chat_label}: {exc}")
        return
    summary.queued.append(commitment.slug)


def _next_touch(instance_dir: Path, tag: str, *, since: datetime | None) -> int:
    count = 0
    if since is None:
        since_utc = datetime.min.replace(tzinfo=timezone.utc)
    else:
        since_utc = _as_utc(since)
    for commitment in pending_with_tag(instance_dir, tag) + archived_with_tag(instance_dir, tag):
        created = _as_utc(commitment.created_at)
        if created >= since_utc and commitment.origin == "reengage":
            count += 1
    return count + 1


def _target_due(
    cfg: ReengageConfig,
    chat: TrackedChat,
    last_user_ts: datetime,
    after_hours: int,
) -> datetime:
    tz = zoneinfo(cfg)
    target = _as_utc(last_user_ts).astimezone(tz) + timedelta(hours=after_hours)
    slots = chat.allowed_slots or cfg.allowed_slots
    candidates = sorted(_parse_hhmm(slot) for slot in slots)
    for day_offset in range(0, 8):
        day = (target + timedelta(days=day_offset)).date()
        for hour, minute in candidates:
            candidate = datetime(
                day.year,
                day.month,
                day.day,
                hour,
                minute,
                tzinfo=tz,
            )
            if candidate < target:
                continue
            if _in_quiet_hours(candidate, cfg):
                continue
            return candidate.astimezone(timezone.utc)
    return target.astimezone(timezone.utc)


def _parse_hhmm(value: str) -> tuple[int, int]:
    match = re.match(r"^(\d{1,2}):(\d{2})$", value.strip())
    if not match:
        raise ValueError(f"invalid HH:MM value: {value}")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"invalid HH:MM value: {value}")
    return hour, minute


def _in_quiet_hours(value: datetime, cfg: ReengageConfig) -> bool:
    start = _parse_hhmm(cfg.quiet_hours.start)
    end = _parse_hhmm(cfg.quiet_hours.end)
    cur = (value.hour, value.minute)
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end


def _slug(chat_id: int, touch_n: int, due: datetime) -> str:
    safe = re.sub(r"[^a-z0-9-]+", "-", str(chat_id).lower()).strip("-") or "chat"
    digest = hashlib.sha1(str(chat_id).encode("utf-8")).hexdigest()[:8]
    stamp = due.astimezone(timezone.utc).strftime("%Y%m%d%H%M")
    return f"reengage-{safe[:18]}-{digest}-t{touch_n}-{stamp}"[:64].rstrip("-")


def _chat_tag(chat_id: int | str) -> str:
    return f"re-engagement:{chat_id}"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
