"""§18 — conversation-scoped compaction orchestration.

`/compact` and `context_exhausted` recovery share one primitive: rotate a
session owner's active native-session mapping. Rotation is the portable hard
bound (§12); native compaction is an optional optimization not implemented
here (providers without it rotate directly, §13).

After a successful compaction the operator notification fires once per
operation, batching every slot that compacted (§18.1).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import notify, telemetry


def owner_key(channel: str, conversation_id: str, brain: str, slot: int) -> str:
    return f"gateway:{channel}:{conversation_id}:{brain}:{slot}"


@dataclass(frozen=True)
class SlotRef:
    brain: str
    slot: int
    session_id: str


def list_conversation_slots(
    conn: sqlite3.Connection, *, channel: str, conversation_id: str
) -> list[SlotRef]:
    rows = conn.execute(
        """
        SELECT brain, slot, session_id FROM sessions
        WHERE channel=? AND conversation_id=?
        ORDER BY brain ASC, slot ASC
        """,
        (channel, conversation_id),
    ).fetchall()
    return [SlotRef(brain=r["brain"], slot=int(r["slot"]), session_id=r["session_id"]) for r in rows]


def rotate_slot(
    conn: sqlite3.Connection,
    *,
    channel: str,
    conversation_id: str,
    brain: str,
    slot: int,
    expected_session_id: str | None = None,
) -> notify.SlotCompaction | None:
    """Clear one slot's active session mapping (rotation), slot-scoped (§12.3).

    Race-safe: if `expected_session_id` is given and no longer matches the
    stored row, the rotation is a no-op (another in-flight event already
    rotated this slot). Returns the before/after summary on success.
    """
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE channel=? AND conversation_id=? AND brain=? AND slot=?",
        (channel, conversation_id, brain.split(":", 1)[0], int(slot)),
    ).fetchone()
    if row is None:
        return None
    if expected_session_id is not None and row["session_id"] != expected_session_id:
        return None

    key = owner_key(channel, conversation_id, brain.split(":", 1)[0], slot)
    before = telemetry.get_telemetry(conn, owner_key=key)
    tokens_before = before.effective_input_tokens if before else None

    conn.execute(
        "DELETE FROM sessions WHERE channel=? AND conversation_id=? AND brain=? AND slot=?",
        (channel, conversation_id, brain.split(":", 1)[0], int(slot)),
    )
    conn.commit()
    telemetry.record_rotation(conn, owner_key=key)
    after = telemetry.get_telemetry(conn, owner_key=key)

    return notify.SlotCompaction(
        brain=brain.split(":", 1)[0],
        slot=slot,
        tokens_before=tokens_before,
        tokens_after=(after.effective_input_tokens or 0) if after else 0,
        method="rotated",
    )


@dataclass(frozen=True)
class CompactionResult:
    compacted: list[notify.SlotCompaction]
    queued: list[SlotRef]
    report: str


def compact_conversation(
    runtime,
    *,
    channel: str,
    conversation_id: str,
    trigger: str,
    busy_slots: set[int] | None = None,
    conversation_label: str | None = None,
) -> CompactionResult:
    """Run `/compact` for one conversation (§18).

    Eligible (non-busy) slots rotate immediately. Busy slots are reported as
    queued — the gateway re-runs maintenance for them after they go idle. A
    single batched operator notification fires when at least one slot
    compacted (§18.1).
    """
    busy = busy_slots or set()
    from .. import queue

    conn = queue.connect(runtime.instance_dir)
    try:
        slots = list_conversation_slots(conn, channel=channel, conversation_id=conversation_id)
        compacted: list[notify.SlotCompaction] = []
        queued: list[SlotRef] = []
        for ref in slots:
            if ref.slot in busy:
                queued.append(ref)
                continue
            result = rotate_slot(
                conn,
                channel=channel,
                conversation_id=conversation_id,
                brain=ref.brain,
                slot=ref.slot,
                expected_session_id=ref.session_id,
            )
            if result is not None:
                compacted.append(result)
                runtime.log(
                    f"context_session_rotated owner={owner_key(channel, conversation_id, ref.brain, ref.slot)} "
                    f"brain={ref.brain} slot={ref.slot} reason={trigger} "
                    f"session={ref.session_id[:8]}",
                    kind="context_session_rotated",
                )
    finally:
        conn.close()

    if compacted:
        notify.notify_compaction(
            runtime,
            trigger=trigger,
            channel=channel,
            conversation_id=conversation_id,
            slots=compacted,
            conversation_label=conversation_label,
        )
    compacted_payload = [
        {
            "owner_kind": "gateway",
            "owner_key": owner_key(channel, conversation_id, item.brain, item.slot),
            "brain": item.brain,
            "slot": item.slot,
            "tokens_before": item.tokens_before,
            "tokens_after": item.tokens_after,
            "method": item.method,
        }
        for item in compacted
    ]
    queued_payload = [
        {
            "owner_kind": "gateway",
            "owner_key": owner_key(channel, conversation_id, ref.brain, ref.slot),
            "brain": ref.brain,
            "slot": ref.slot,
            "session_id_prefix": ref.session_id[:8],
        }
        for ref in queued
    ]
    for ref in queued:
        runtime.log(
            f"context_compaction_deferred channel={channel} conversation_id={conversation_id} "
            f"brain={ref.brain} slot={ref.slot} trigger={trigger}",
            kind="context_compaction_deferred",
        )
    runtime.log(
        f"context_compaction trigger={trigger} channel={channel} "
        f"conversation_id={conversation_id} rotated={len(compacted)} queued={len(queued)}",
        kind="context_compaction",
        trigger=trigger,
        channel=channel,
        conversation_id=conversation_id,
        slots_rotated=len(compacted),
        slots_queued=len(queued),
        compacted_slots=compacted_payload,
        queued_slots=queued_payload,
    )

    report = _render_report(compacted, queued)
    return CompactionResult(compacted=compacted, queued=queued, report=report)


def _render_report(
    compacted: list[notify.SlotCompaction], queued: list["SlotRef"]
) -> str:
    if not compacted and not queued:
        return "No active brain session to compact for this conversation."
    lines = [f"Context maintenance complete. Rotated {len(compacted)} slot(s); queued {len(queued)}."]
    for s in compacted:
        lines.append(
            f"{s.brain} slot {s.slot}: "
            f"{notify._fmt_tokens(s.tokens_before)} → {notify._fmt_tokens(s.tokens_after)} tokens, rotated."
        )
    for ref in queued:
        lines.append(
            f"{ref.brain} slot {ref.slot}: busy — maintenance queued, runs after the slot goes idle."
        )
    return "\n".join(lines)
