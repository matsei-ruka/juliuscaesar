"""Supervisor tick orchestrator.

Phase 1: snapshot + classify + log.
Phase 2: render + send/edit cards via channel delivery. Loop guard: never
writes to ``state/transcripts/``.
Phase 3: AI narrator — cheap model call fills "Last signal" card field.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from gateway import queue

from .cards import Card, render_card, render_final_card
from .config import SupervisorConfig, load_config as load_supervisor_config
from .delivery import (
    edit_card_discord,
    edit_card_slack,
    edit_card_telegram,
    send_card_discord,
    send_card_slack,
    send_card_telegram,
)
from .models import EventSnapshot, TickResult
from .narrator import narrate
from .recovery import apply_recovery, decide as recovery_decide, escalate_to_failed, load_patterns
from .snapshot import build_snapshots
from .state import RECOVERY_STATE_TTL_SECONDS, EventState, SupervisorState


LogFn = Callable[[str], None]


def run_tick(
    instance_dir: Path,
    *,
    dry_run: bool = False,
    log: LogFn | None = None,
    sender: "CardSender | None" = None,
) -> TickResult:
    """Run one supervisor tick.

    ``sender`` is an injection point used by tests to capture card I/O without
    hitting Telegram. Defaults to the real Telegram delivery functions.
    """
    log = log or (lambda _: None)
    cfg = load_supervisor_config(instance_dir)
    if not cfg.enabled:
        return TickResult(enabled=False)

    sender = sender or _MultiChannelSender()

    now = datetime.now(timezone.utc)
    state = SupervisorState.load(instance_dir)

    if state.last_tick_at and (
        now.timestamp() - state.last_tick_at
    ) < cfg.tick_interval_seconds:
        return TickResult(enabled=True)

    _write_log(instance_dir, {"kind": "supervisor_tick_begin", "ts": now.isoformat()})

    try:
        snapshots = build_snapshots(instance_dir, cfg, now=now)
    except Exception as exc:  # noqa: BLE001
        _write_log(
            instance_dir,
            {"kind": "supervisor_tick_error", "ts": now.isoformat(), "error": str(exc)},
        )
        return TickResult(enabled=True, error=str(exc))

    result = TickResult(enabled=True, snapshots=snapshots)
    active_ids = {s.event.id for s in snapshots}

    # 1. Close cards for events that finished since last tick.
    _finalize_completed(
        instance_dir, cfg, state, active_ids,
        now=now, dry_run=dry_run, sender=sender, log=log,
    )

    # 2. Silent recovery — re-queue events whose adapter PID is gone.
    recovered_ids: set[int] = set()
    if cfg.recovery_enabled:
        patterns = load_patterns(instance_dir, cfg.recovery_patterns)
        for snap in snapshots:
            ev_state = state.event(snap.event.id)
            decision = recovery_decide(snap, ev_state, cfg, patterns=patterns)

            if decision.triggered:
                if not dry_run:
                    ok = apply_recovery(
                        instance_dir,
                        snap.event.id,
                        decision,
                        log=log,
                        expected_locked_by=snap.event.locked_by,
                    )
                    if ok:
                        ev_state.recovery_attempts += 1
                        ev_state.pinned_until = (
                            now.timestamp() + RECOVERY_STATE_TTL_SECONDS
                        )
                        recovered_ids.add(snap.event.id)
                        result.recoveries.append(
                            {
                                "event_id": snap.event.id,
                                "class": decision.failure_class,
                                "drop_resume_session": decision.drop_resume_session,
                                "available_in_seconds": decision.available_in_seconds,
                                "attempt": ev_state.recovery_attempts,
                            }
                        )
                        _write_log(
                            instance_dir,
                            {
                                "kind": "supervisor_recovery_triggered",
                                "ts": now.isoformat(),
                                "event_id": snap.event.id,
                                "class": decision.failure_class,
                                "drop_resume_session": decision.drop_resume_session,
                                "available_in_seconds": decision.available_in_seconds,
                                "attempt": ev_state.recovery_attempts,
                            },
                        )

            elif decision.reason == "max_recovery_attempts_exceeded" and not dry_run:
                # Phase 6: exhausted retries — escalate to failed + watchdog handoff.
                ok = escalate_to_failed(instance_dir, snap.event.id, log=log)
                if ok:
                    ev_state.escalated = True
                    recovered_ids.add(snap.event.id)
                    result.recoveries.append(
                        {
                            "event_id": snap.event.id,
                            "class": "escalated",
                            "drop_resume_session": False,
                            "available_in_seconds": 0,
                            "attempt": ev_state.recovery_attempts,
                        }
                    )
                    _write_log(
                        instance_dir,
                        {
                            "kind": "supervisor_recovery_escalated",
                            "ts": now.isoformat(),
                            "event_id": snap.event.id,
                            "attempt": ev_state.recovery_attempts,
                        },
                    )

    # 3. Send/edit cards for events still running and past threshold.
    tick_narrator_calls = 0
    for snap in snapshots:
        if snap.event.id in recovered_ids:
            # Event was just re-queued; skip card emit to avoid stale render.
            continue
        skip_reason = _should_skip(snap, cfg, state, now)
        if skip_reason:
            result.skipped.append({"event_id": snap.event.id, "reason": skip_reason})
            _write_log(
                instance_dir,
                {
                    "kind": "supervisor_card_skipped",
                    "ts": now.isoformat(),
                    "event_id": snap.event.id,
                    "reason": skip_reason,
                },
            )
            continue

        log(
            f"supervisor event={snap.event.id} brain={snap.brain_spec} "
            f"age={snap.age_seconds:.0f}s phase={snap.phase.phase} "
            f"emoji={snap.phase.emoji} pid_alive={snap.adapter.pid_alive}"
        )

        if not dry_run:
            narrator_budget = tick_narrator_calls < cfg.narrator_calls_per_tick_max
            used = _render_and_send(
                instance_dir, cfg, state, snap,
                now=now, sender=sender, log=log,
                narrator_budget=narrator_budget,
            )
            if used:
                tick_narrator_calls += 1

    if not dry_run:
        state.prune(active_ids, now=now.timestamp())
        state.last_tick_at = now.timestamp()
        state.save(instance_dir)

    _write_log(
        instance_dir,
        {
            "kind": "supervisor_tick_end",
            "ts": now.isoformat(),
            "qualifying": len(snapshots),
            "skipped": len(result.skipped),
        },
    )
    return result


def _should_skip(
    snap: EventSnapshot,
    cfg: SupervisorConfig,
    state: SupervisorState,
    now: datetime,
) -> str:
    if snap.worker_linked:
        return "worker_linked"

    source = snap.event.source or ""
    if not cfg.channel_enabled(source):
        return f"channel_disabled:{source}"

    # Group chats opt-in
    chat_type = str(snap.meta.get("chat_type") or "")
    if chat_type in ("group", "supergroup", "channel") and not cfg.groups_enabled:
        return "group_chat"

    ev_state = state.events.get(str(snap.event.id))
    if ev_state is None:
        return ""

    # Backoff: phase unchanged AND within min_card_interval
    age_since_last_card = now.timestamp() - ev_state.last_card_at
    if (
        ev_state.last_phase == snap.phase.phase
        and age_since_last_card < cfg.min_card_interval_seconds
    ):
        return "backoff"

    if ev_state.card_count >= cfg.max_cards_per_event:
        return "max_cards"

    return ""


def _render_and_send(
    instance_dir: Path,
    cfg: SupervisorConfig,
    state: SupervisorState,
    snap: EventSnapshot,
    *,
    now: datetime,
    sender: "CardSender",
    log: LogFn,
    narrator_budget: bool = True,
) -> bool:
    """Render and send/edit a progress card. Returns True if narrator was called."""
    ev_state = state.event(snap.event.id)

    # First card sets the language; subsequent cards keep the same language so
    # editing a message can't flip mid-stream.
    if ev_state.card_count == 0:
        ev_state.language = snap.language
        ev_state.first_card_at = now.timestamp()

    # Narrator: call if budget allows and event hasn't hit its cap.
    narrator_called = False
    narration_text = ""
    event_narrator_cap = ev_state.narration_count < cfg.narrator_calls_per_event_max
    if narrator_budget and event_narrator_cap:
        result = narrate(
            snap, ev_state, cfg.narrator_brain, instance_dir, log=log,
        )
        narration_text = result.text
        narrator_called = result.from_model
        if result.from_model:
            ev_state.narration_count += 1
            ev_state.last_narration = result.text
        _write_log(
            instance_dir,
            {
                "kind": "supervisor_narrator_call",
                "ts": now.isoformat(),
                "event_id": snap.event.id,
                "from_model": result.from_model,
                "narration": result.text,
            },
        )

    title = _title_from_meta(snap.meta, snap.event.content)
    card = render_card(
        title=title,
        phase=snap.phase,
        elapsed_seconds=snap.age_seconds,
        activity_age_seconds=snap.adapter.activity_age_seconds,
        narration=narration_text,
        language=ev_state.language,
    )

    source = snap.event.source or "telegram"
    if not _delivery_address(source, snap.meta):
        return narrator_called

    if ev_state.channel_message_id:
        ok = sender.edit(
            instance_dir=instance_dir,
            source=source,
            meta=snap.meta,
            message_id=ev_state.channel_message_id,
            card=card,
            log=log,
        )
        action = "edit"
    else:
        mid = sender.send(
            instance_dir=instance_dir,
            source=source,
            meta=snap.meta,
            card=card,
            log=log,
        )
        if mid:
            ev_state.channel_message_id = mid
        ok = mid is not None
        action = "send"

    if ok:
        ev_state.last_card_at = now.timestamp()
        ev_state.last_phase = snap.phase.phase
        ev_state.card_count += 1

    _write_log(
        instance_dir,
        {
            "kind": "supervisor_card_rendered",
            "ts": now.isoformat(),
            "event_id": snap.event.id,
            "action": action,
            "phase": snap.phase.phase,
            "emoji": snap.phase.emoji,
            "language": ev_state.language,
            "message_id": ev_state.channel_message_id,
            "ok": ok,
        },
    )
    return narrator_called


def _finalize_completed(
    instance_dir: Path,
    cfg: SupervisorConfig,
    state: SupervisorState,
    active_ids: set[int],
    *,
    now: datetime,
    dry_run: bool,
    sender: "CardSender",
    log: LogFn,
) -> None:
    """Render ✅ for events that had a card and have since completed.

    A "completed" event is one that:
    - had a ``channel_message_id`` stored in state (got at least one card),
    - is no longer in the active snapshot set (not running anymore),
    - is in status ``done`` per queue.db (success path).

    Events in ``failed`` status are not finalized here — Phase 5 (silent
    recovery) owns that case.
    """
    candidate_ids: list[int] = []
    for k, ev_state in state.events.items():
        try:
            eid = int(k)
        except ValueError:
            continue
        if eid in active_ids:
            continue
        if not ev_state.channel_message_id:
            continue
        candidate_ids.append(eid)

    if not candidate_ids:
        return

    statuses = _fetch_statuses(instance_dir, candidate_ids)

    for eid in candidate_ids:
        status_row = statuses.get(eid)
        if status_row is None:
            continue
        if status_row.get("status") != "done":
            continue
        ev_state = state.events[str(eid)]
        title = _title_from_status_row(status_row)
        elapsed = _elapsed_from_status_row(status_row, now)
        card = render_final_card(
            title=title,
            elapsed_seconds=elapsed,
            language=ev_state.language or "en",
        )
        if dry_run:
            continue
        source = str(status_row.get("source") or "telegram")
        meta = status_row.get("meta") or {}
        if not _delivery_address(source, meta):
            continue
        ok = sender.edit(
            instance_dir=instance_dir,
            source=source,
            meta=meta,
            message_id=ev_state.channel_message_id,
            card=card,
            log=log,
        )
        _write_log(
            instance_dir,
            {
                "kind": "supervisor_card_finalized",
                "ts": now.isoformat(),
                "event_id": eid,
                "message_id": ev_state.channel_message_id,
                "ok": ok,
            },
        )
        # Drop the event from state — final card sent, no more updates.
        del state.events[str(eid)]


def _fetch_statuses(instance_dir: Path, event_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Look up status + meta for a set of event IDs."""
    if not event_ids:
        return {}
    placeholders = ",".join("?" for _ in event_ids)
    conn = queue.connect(instance_dir)
    try:
        rows = conn.execute(
            f"SELECT id, source, status, started_at, finished_at, meta, content "
            f"FROM events WHERE id IN ({placeholders})",
            event_ids,
        ).fetchall()
    finally:
        conn.close()

    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        meta_raw = row["meta"]
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        out[int(row["id"])] = {
            "source": row["source"] or "telegram",
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "content": row["content"] or "",
            "meta": meta if isinstance(meta, dict) else {},
        }
    return out


def _title_from_status_row(row: dict[str, Any]) -> str:
    meta = row.get("meta") or {}
    if isinstance(meta, dict):
        for key in ("text", "transcription"):
            v = meta.get(key)
            if isinstance(v, str) and v.strip():
                return v
    return str(row.get("content") or "")


def _elapsed_from_status_row(row: dict[str, Any], now: datetime) -> float:
    started = row.get("started_at")
    finished = row.get("finished_at")
    if started and finished:
        try:
            t0 = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
            return max(0.0, t1.timestamp() - t0.timestamp())
        except ValueError:
            pass
    if started:
        try:
            t0 = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            return max(0.0, now.timestamp() - t0.timestamp())
        except ValueError:
            pass
    return 0.0


def _title_from_meta(meta: dict[str, Any], content: str) -> str:
    for key in ("text", "transcription"):
        v = meta.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return content or ""


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _write_log(instance_dir: Path, record: dict) -> None:
    log_path = instance_dir / "state" / "logs" / "supervisor.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _delivery_address(source: str, meta: dict) -> str:
    """Return the channel address for delivery, or empty if missing."""
    if source == "slack":
        return str(meta.get("channel") or "")
    if source == "discord":
        return str(meta.get("channel_id") or "")
    return str(meta.get("chat_id") or "")


# --- Sender abstraction (testable) ---

class CardSender:
    """Pluggable card sender. Production routes per source; tests use a fake."""

    def send(
        self,
        *,
        instance_dir: Path,
        source: str,
        meta: dict,
        card: Card,
        log: LogFn,
    ) -> str | None:
        raise NotImplementedError

    def edit(
        self,
        *,
        instance_dir: Path,
        source: str,
        meta: dict,
        message_id: str,
        card: Card,
        log: LogFn,
    ) -> bool:
        raise NotImplementedError


class _MultiChannelSender(CardSender):
    def send(
        self,
        *,
        instance_dir: Path,
        source: str,
        meta: dict,
        card: Card,
        log: LogFn,
    ) -> str | None:
        if source == "slack":
            ts = send_card_slack(
                instance_dir=instance_dir,
                channel=str(meta.get("channel") or ""),
                card=card,
                thread_ts=str(meta.get("thread_ts") or meta.get("ts") or "") or None,
                log=log,
            )
            return ts
        if source == "discord":
            return send_card_discord(
                instance_dir=instance_dir,
                channel_id=str(meta.get("channel_id") or ""),
                card=card,
                reply_to_message_id=str(meta.get("reply_to_message_id") or "") or None,
                log=log,
            )
        # default: telegram
        mid = send_card_telegram(
            instance_dir=instance_dir,
            chat_id=str(meta.get("chat_id") or ""),
            card=card,
            reply_to_message_id=_int_or_none(meta.get("message_id")),
            message_thread_id=_int_or_none(meta.get("message_thread_id")),
            log=log,
        )
        return str(mid) if mid is not None else None

    def edit(
        self,
        *,
        instance_dir: Path,
        source: str,
        meta: dict,
        message_id: str,
        card: Card,
        log: LogFn,
    ) -> bool:
        if source == "slack":
            return edit_card_slack(
                instance_dir=instance_dir,
                channel=str(meta.get("channel") or ""),
                ts=message_id,
                card=card,
                log=log,
            )
        if source == "discord":
            return edit_card_discord(
                instance_dir=instance_dir,
                channel_id=str(meta.get("channel_id") or ""),
                message_id=message_id,
                card=card,
                log=log,
            )
        # default: telegram
        return edit_card_telegram(
            instance_dir=instance_dir,
            chat_id=str(meta.get("chat_id") or ""),
            message_id=_int_or_none(message_id) or 0,
            card=card,
            log=log,
        )
