"""Supervisor tick orchestrator.

Phase 1: snapshot + classify + log.
Phase 2: render + send/edit cards via channel delivery. Loop guard: never
writes to ``state/transcripts/``.
Phase 3: AI narrator — cheap model call fills "Last signal" card field.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from company import supervisor_conf as company_conf
from company.supervisor_reporter import Reporter as CompanyReporter
from gateway import queue

from .cards import Card, render_card, render_stopped_card
from .config import SupervisorConfig, load_config as load_supervisor_config
from .delivery import (
    delete_card_discord,
    delete_card_slack,
    delete_card_telegram,
    edit_card_discord,
    edit_card_slack,
    edit_card_telegram,
    send_card_discord,
    send_card_slack,
    send_card_telegram,
)
from .models import EventSnapshot, TickResult
from .narrator import generate_title, narrate
from .recovery import apply_recovery, decide as recovery_decide, escalate_to_failed, load_patterns
from .snapshot import build_snapshots
from .state import RECOVERY_STATE_TTL_SECONDS, EventState, SupervisorState


LogFn = Callable[[str], None]


# Process-wide cache: build the company reporter once and reuse the same
# instance_boot_id for every tick within a gateway lifetime. Keyed by
# instance_dir so multi-instance test runs don't share boot ids.
_REPORTER_CACHE: dict[str, "CompanyReporter | None"] = {}


def _build_default_reporter(instance_dir: Path, log: LogFn) -> "CompanyReporter | None":
    """Return a process-scoped Reporter (or None when disabled / misconfigured).

    Reads ``ops/the_company.yaml`` and caches the result. If the file or key
    is missing, returns None — the supervisor then skips reporting silently.
    """
    key = str(Path(instance_dir).resolve())
    if key in _REPORTER_CACHE:
        return _REPORTER_CACHE[key]
    cfg = company_conf.load(Path(instance_dir))
    if cfg.disabled:
        _REPORTER_CACHE[key] = None
        return None
    boot_id = str(uuid.uuid4())
    reporter = CompanyReporter(
        api_url=cfg.api_url,
        agent_id=cfg.agent_id,
        api_key=cfg.api_key,
        instance_boot_id=boot_id,
        log_fn=log,
    )
    _REPORTER_CACHE[key] = reporter
    return reporter


# Sentinel so callers can explicitly pass ``reporter=None`` to disable
# reporting (e.g. in tests) without us falling back to the default loader.
_USE_DEFAULT_REPORTER: Any = object()


def run_tick(
    instance_dir: Path,
    *,
    dry_run: bool = False,
    log: LogFn | None = None,
    sender: "CardSender | None" = None,
    reporter: "CompanyReporter | None | Any" = _USE_DEFAULT_REPORTER,
) -> TickResult:
    """Run one supervisor tick.

    ``sender`` is an injection point used by tests to capture card I/O without
    hitting Telegram. Defaults to the real Telegram delivery functions.

    ``reporter`` is an injection point for the the-company worker reporter.
    Pass an explicit ``None`` to disable reporting, a fake to capture calls
    in tests, or omit to load from ``ops/the_company.yaml``.
    """
    log = log or (lambda _: None)
    cfg = load_supervisor_config(instance_dir)
    if not cfg.enabled:
        return TickResult(enabled=False)

    sender = sender or _MultiChannelSender()
    if reporter is _USE_DEFAULT_REPORTER:
        reporter = _build_default_reporter(instance_dir, log)

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

    # 1. Close cards for events that finished since last tick. Same pass
    #    also fires worker.finished to the-company for any event we'd
    #    previously announced as running.
    _finalize_completed(
        instance_dir, cfg, state, active_ids,
        now=now, dry_run=dry_run, sender=sender, log=log,
        reporter=reporter,
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
                # Before transitioning the row, close any open progress card
                # with the neutral ⏹ stopped layout so the user doesn't see the
                # last "📖 reading" frame indefinitely (Bug #11). Drop the
                # message_id afterwards so _finalize_completed doesn't try to
                # edit a card that's already been finalized here.
                if ev_state.channel_message_id:
                    source = snap.event.source or "telegram"
                    address = _delivery_address(source, snap.meta)
                    if address:
                        title = _title_from_meta(snap.meta, snap.event.content)
                        elapsed = snap.age_seconds
                        stopped_card = render_stopped_card(
                            title=title,
                            elapsed_seconds=elapsed,
                            language=ev_state.language or "en",
                        )
                        sender.edit(
                            instance_dir=instance_dir,
                            source=source,
                            meta=snap.meta,
                            message_id=ev_state.channel_message_id,
                            card=stopped_card,
                            log=log,
                        )
                    ev_state.channel_message_id = None
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
        # Announce worker.started to the-company on first sight, regardless
        # of card-emit gating (group chat, voice, etc). The dashboard wants
        # the whole truth of what the gateway is working on. Skipped in
        # dry_run for parity with finalize and to avoid real-world side
        # effects from a planning-only tick.
        ev_state = state.event(snap.event.id)
        if (
            not dry_run
            and reporter is not None
            and not ev_state.company_reported_started
        ):
            tick_narrator_calls += _report_started(
                instance_dir, cfg, ev_state, snap, now=now,
                reporter=reporter, log=log,
                narrator_budget=tick_narrator_calls < cfg.narrator_calls_per_tick_max,
            )

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

    # Narrator: always called (fast path for empty stderr), AI gated by budget.
    narrator_called = False
    event_narrator_cap = ev_state.narration_count < cfg.narrator_calls_per_event_max
    result = narrate(
        snap,
        ev_state,
        cfg.narrator_brain,
        instance_dir,
        narrator_budget=narrator_budget and event_narrator_cap,
        log=log,
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

    # Generate AI activity title on first card only (stored in ev_state).
    if not ev_state.title and narrator_budget:
        raw_content = snap.event.content or _title_from_meta(snap.meta, snap.event.content)
        generated = generate_title(
            raw_content,
            cfg.narrator_brain,
            instance_dir,
            language=ev_state.language,
            log=log,
        )
        if generated:
            ev_state.title = generated

    title = ev_state.title or _title_from_meta(snap.meta, snap.event.content)
    card = render_card(
        title=title,
        phase=snap.phase,
        elapsed_seconds=snap.age_seconds,
        narration=narration_text,
        language=ev_state.language,
        slot=snap.slot,
        max_concurrent=2 if snap.slot is not None else 1,
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
        if not ok:
            # Edit failed (message deleted, expired edit window, daemon restart
            # state mismatch, etc.). Delete the stale card first so it doesn't
            # linger as a duplicate alongside the fresh card we're about to send
            # (Bug #9, partner to Bug #8).
            stale_id = ev_state.channel_message_id
            ev_state.channel_message_id = None
            sender.delete(
                instance_dir=instance_dir,
                source=source,
                meta=snap.meta,
                message_id=stale_id,
                log=log,
            )
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
            action = "edit_then_send"
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


def _report_started(
    instance_dir: Path,
    cfg: SupervisorConfig,
    ev_state: EventState,
    snap: EventSnapshot,
    *,
    now: datetime,
    reporter: "CompanyReporter",
    log: LogFn,
    narrator_budget: bool,
) -> int:
    """Fire worker.started for ``snap`` once, caching brain/model/started_at.

    Topic resolution priority:
    1. Cached AI title on ``ev_state.title`` (set by a prior tick or here).
    2. Fresh ``generate_title(...)`` call when there's narrator budget left.
    3. Best-effort fallback from event meta (text / transcription / content).

    Returns 1 if it spent a narrator call, else 0 — so the caller can
    track the per-tick budget.
    """
    narrator_used = 0
    raw_content = snap.event.content or _title_from_meta(snap.meta, snap.event.content)

    if not ev_state.title and narrator_budget and raw_content:
        generated = generate_title(
            raw_content,
            cfg.narrator_brain,
            instance_dir,
            language=snap.language,
            log=log,
        )
        if generated:
            ev_state.title = generated
            narrator_used = 1

    topic = ev_state.title or _title_from_meta(snap.meta, snap.event.content) or ""

    started_at = _started_at_from_snapshot(snap, fallback=now)
    started_iso = started_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    ok = reporter.report_started(
        event_id=snap.event.id,
        topic=topic,
        started_at=started_at,
        brain=snap.brain or None,
        model=snap.model,
    )
    if ok:
        ev_state.company_reported_started = True
        ev_state.company_brain = snap.brain or ""
        ev_state.company_model = snap.model or ""
        ev_state.company_started_at_iso = started_iso
    # On failure we leave company_reported_started=False so the next tick retries.
    return narrator_used


def _started_at_from_snapshot(snap: EventSnapshot, *, fallback: datetime) -> datetime:
    """Parse an event's started_at into an aware UTC datetime, with fallback."""
    raw = snap.event.started_at or snap.event.received_at
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            pass
    return fallback


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
    reporter: "CompanyReporter | None" = None,
) -> None:
    """Render a terminal card for events that had a card and have since
    finished (Bug #10 / #11).

    A "finished" event is one that:
    - had a ``channel_message_id`` stored in state (got at least one card),
    - is no longer in the active snapshot set (not running anymore),
    - is in a terminal status: ``done`` → ✅ final card, ``failed`` or
      ``escalated`` → neutral ⏹ stopped card (no crash/error text per spec).
    """
    # Candidates are any tracked EventState no longer in active_ids. Two
    # things can happen per candidate:
    #   - it has a channel_message_id → delete the progress card,
    #   - it was previously announced to the-company → fire worker.finished.
    # Both are independent: an event may have one, the other, or both.
    candidate_ids: list[int] = []
    for k, ev_state in state.events.items():
        try:
            eid = int(k)
        except ValueError:
            continue
        if eid in active_ids:
            continue
        if not ev_state.channel_message_id and not ev_state.company_reported_started:
            continue
        candidate_ids.append(eid)

    if not candidate_ids:
        return

    statuses = _fetch_statuses(instance_dir, candidate_ids)

    for eid in candidate_ids:
        status_row = statuses.get(eid)
        if status_row is None:
            continue
        status = status_row.get("status")
        if status not in ("done", "failed", "escalated"):
            continue
        ev_state = state.events[str(eid)]
        if dry_run:
            continue

        # --- Card delete (only if we have one) ---
        if ev_state.channel_message_id:
            source = str(status_row.get("source") or "telegram")
            meta = status_row.get("meta") or {}
            if _delivery_address(source, meta):
                ok = sender.delete(
                    instance_dir=instance_dir,
                    source=source,
                    meta=meta,
                    message_id=ev_state.channel_message_id,
                    log=log,
                )
                _write_log(
                    instance_dir,
                    {
                        "kind": "supervisor_card_finalized",
                        "ts": now.isoformat(),
                        "event_id": eid,
                        "status": status,
                        "message_id": ev_state.channel_message_id,
                        "action": "delete",
                        "ok": ok,
                    },
                )

        # --- the-company finalize ---
        if (
            reporter is not None
            and ev_state.company_reported_started
            and not ev_state.company_reported_finished
        ):
            started_at = _parse_iso(ev_state.company_started_at_iso) or _parse_iso(
                str(status_row.get("started_at") or "")
            ) or now
            finished_at = _parse_iso(str(status_row.get("finished_at") or "")) or now
            ok = reporter.report_finished(
                event_id=eid,
                started_at=started_at,
                finished_at=finished_at,
                brain=ev_state.company_brain or None,
                model=ev_state.company_model or None,
            )
            if ok:
                ev_state.company_reported_finished = True

        # Drop the event from state — finalized in both channels.
        del state.events[str(eid)]


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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

    def delete(
        self,
        *,
        instance_dir: Path,
        source: str,
        meta: dict,
        message_id: str,
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

    def delete(
        self,
        *,
        instance_dir: Path,
        source: str,
        meta: dict,
        message_id: str,
        log: LogFn,
    ) -> bool:
        if source == "slack":
            return delete_card_slack(
                instance_dir=instance_dir,
                channel=str(meta.get("channel") or ""),
                ts=message_id,
                log=log,
            )
        if source == "discord":
            return delete_card_discord(
                instance_dir=instance_dir,
                channel_id=str(meta.get("channel_id") or ""),
                message_id=message_id,
                log=log,
            )
        # default: telegram
        return delete_card_telegram(
            instance_dir=instance_dir,
            chat_id=str(meta.get("chat_id") or ""),
            message_id=_int_or_none(message_id) or 0,
            log=log,
        )
