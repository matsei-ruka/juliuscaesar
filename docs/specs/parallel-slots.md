# Parallel slots — concurrent brain dispatch per conversation

**Status:** draft (2026-05-21)
**Owner:** Luca + Rachel
**Touches:** `lib/gateway/runtime.py`, `lib/gateway/sessions.py`, `lib/gateway/brains/base.py`, `lib/supervisor/cards.py`, `lib/gateway/channels/telegram.py`

## Problem

Today the gateway processes events strictly serially per `(channel, conversation_id)`. When the user sends msg B while msg A is still running, B sits in the queue with a 👀 reaction and waits — even if B is trivial ("what time is it?") and A is a 5-minute deck render.

Asymmetric workloads (fast follow-ups interleaved with slow tasks) suffer the most. The user has no way to get a quick answer without waiting on the slow one.

## Goal

Allow N concurrent brain invocations per conversation, keeping per-slot continuity, so unrelated quick messages don't queue behind long ones.

## Non-goals

- Parallel processing across different conversations (already works).
- Cross-instance parallelism.
- Replacing the existing worker spawn mechanism (`jc workers spawn`) — that stays for explicit dev tasks.

## Model

### Slots

A **slot** is a long-lived parallel session chain for a `(channel, conversation_id, brain)` triple. Indexed `0..N-1`. `N = max_concurrent`.

- `N = 1` (default) → identical to today's serial behavior.
- `N = 3` → up to 3 concurrent brain invocations for the same conversation.

Each slot persists its own session UUID; slots accumulate independent history. A free slot is one with no in-flight event.

### DB schema migration

Current:
```sql
CREATE TABLE sessions (
    ...
    UNIQUE(channel, conversation_id, brain)
);
```

New:
```sql
CREATE TABLE sessions (
    ...
    slot INTEGER NOT NULL DEFAULT 0,
    UNIQUE(channel, conversation_id, brain, slot)
);
```

Migration: `ALTER TABLE sessions ADD COLUMN slot INTEGER NOT NULL DEFAULT 0`. Existing rows become slot 0 — backward-compatible with N=1.

Example rows after migration:
```
telegram | 28547271 | 0 | claude | 99cb1dc1... | 2026-05-21T10:22:51Z
telegram | 28547271 | 1 | claude | 7455cb1dc1... | 2026-05-21T10:25:01Z
telegram | 28547271 | 2 | claude | 88aadc1...   | 2026-05-21T10:25:14Z
```

### Dispatch logic

When a new event arrives for `(channel, conv)`:

1. **Compute slot occupancy.** Query in-flight events from the queue keyed by `(channel, conv, slot)`. A slot is busy if any event with that key is in `claimed` or `running` state.

2. **Run the relatedness classifier.** Cheap openrouter call (`deepseek-v4-flash` or similar): given the new event's content and a short summary of each slot's recent activity (last ~3 user turns), return one of:
   - `related:<slot_id>` — continue this slot
   - `unrelated`

3. **Pick a slot:**
   - **Related to busy slot** → enqueue behind that slot (preserves order).
   - **Related to free slot** → resume that slot.
   - **Unrelated + any free slot exists** → pick the slot with the oldest last-turn timestamp (least-recently-used). Tie-break: lowest slot id.
   - **All busy + no related slot** → enqueue on slot 0 (the canonical "main" lane).

4. **Inject global recent context (Option A).** Regardless of which slot is picked, prepend the last `transcript_context_lines: 20` (configurable) global transcript lines to the prompt. This keeps each slot loosely aware of activity in other slots, so slot 1 doesn't answer blind when the user references something slot 0 just did.

5. **Spawn brain.** Same brain config; `--session` from the chosen slot's row (or no resume on first turn). Capture and upsert the session_id back to that slot.

### Reply threading

The brain's reply is delivered via Telegram with `reply_to_message_id` = the event's original `message_id`. Telegram already supports this; just set the field on outbound delivery for all parallel events (also harmless for serial).

This makes ordering visually unambiguous when slot 1 finishes before slot 0.

## UX changes

### Reactions

Today: 👀 when an event sits in queue.

New mapping:
- `🏃` — parallel slot picked, brain running now.
- `👀` — queued behind a busy slot (waiting).

Cleared on completion as today.

### Supervisor cards

Today each running event gets a single sticky card with format:
```
🟢 <title>

<minute-bar> · <activity-bar>
```

With multiple parallel cards in the same conversation, the leading emoji (a "LED" — 🟢 starting, 💭 thinking, 📖 reading, etc.) should be replaced with the **slot number emoji** when `N > 1`:

- Slot 0 → 0️⃣
- Slot 1 → 1️⃣
- Slot 2 → 2️⃣
- ...up to 9️⃣ (cap N at 10 for now)

Phase emoji moves to the second line, embedded in the activity line:
```
1️⃣ <title>

💭 <phase_label> · <minute-bar> · <activity-bar>
```

When `N = 1` (default), keep current rendering (phase emoji as card prefix) — no regression for instances that don't opt in.

Final card (✅) and stopped card (⏹) unchanged.

### Footer

Footer already shows `brain · sess <id> · <time>`. Add slot when N > 1:
```
⚙️ claude:opus · slot 1 · sess 7455cb1d… · 03:21
```

Hidden when N = 1.

## Config

New keys in `ops/gateway.yaml`:

```yaml
parallel:
  max_concurrent: 1          # default — current serial behavior
  transcript_context_lines: 20
  classifier:
    backend: openrouter      # reuse triage backend
    model: deepseek/deepseek-v4-flash
    timeout_seconds: 3
    cache_ttl_seconds: 30    # memoize per-event-pair to avoid redundant calls
```

All optional. Omitting the block = current behavior.

## Concurrency safety

Risks reviewed; corruption risk judged low:

- **Transcript append:** each line is atomic JSON, single `write()` call. Interleaving safe.
- **SQLite writes:** WAL mode handles concurrent writers natively.
- **Memory file writes:** memory edits are full-file rewrites via tools (Read+Edit). Two brains rewriting the same file simultaneously is a race but rare in practice. Accept the risk; if it bites, add a per-file lock in a follow-up.
- **Brain process spawn:** each invocation is a separate subprocess with its own stdin/stdout/stderr pipes. No shared state across processes.

## Migration & rollout

1. Schema migration script: `lib/gateway/migrations/2026-05-22_sessions_slot.sql`.
2. `lib/gateway/sessions.py` — `get_session` and `upsert_session` gain `slot: int = 0` kwarg.
3. `lib/gateway/runtime.py` — `_resume_id` and `_record_session` pass slot through. New `_pick_slot(event)` method.
4. `lib/supervisor/cards.py` — `render_card` gains `slot: int | None = None` kwarg; when set + N > 1, swap leading emoji.
5. `lib/gateway/channels/telegram.py` — reaction emoji map updated (🏃 vs 👀).

Backward compat: instances with no `parallel:` block keep `max_concurrent=1`, slot defaults to 0 everywhere, behavior unchanged.

## Open questions

- **Classifier prompt design.** Need to draft + test on real conversation transcripts. False positives (claiming "related" when unrelated) hurt latency; false negatives (claiming "unrelated" when related) cause context loss.
- **Slot retirement.** Should idle slots eventually be retired (session row deleted) to keep DB clean? Proposal: GC slot rows untouched for > 30 days, exempt slot 0.
- **Cross-brain slots.** Today different brains for the same conv get separate rows. Slots are per-brain. If user switches brain mid-conversation, the new brain starts fresh slots — no resume from a different brain's history. Acceptable; matches today's behavior.

## Test plan

- Unit: `_pick_slot` matrix (all combinations of busy/free + classifier verdicts).
- Unit: sessions.py upsert/get with slot kwarg.
- Unit: card renderer with slot kwarg.
- Integration: spin gateway with `max_concurrent: 2`, dispatch two unrelated events, assert both run concurrently.
- Integration: dispatch related follow-up while parallel running, assert it queues behind the right slot.
- Migration: existing DB with N rows → run migration → N rows with slot=0, query still works.
