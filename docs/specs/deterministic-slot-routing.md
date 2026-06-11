# Deterministic slot routing — replace the relatedness classifier

## Problem

Parallel dispatch (`max_concurrent > 1`) fragments a single human conversation.
`runtime._pick_slot` asks an LLM relatedness classifier
(`_classify_slot_affinity`) whether a new message belongs to a busy slot. For a
normal back-and-forth the classifier keeps returning "unrelated" → each message
lands on a fresh `_lru_free_slot` → fresh session → the brain answers with **no
knowledge of the conversation context**. Observed on Livia (.125,
`max_concurrent: 8`), log signature `slot pick id=N unrelated → lru-free=7`.

Root issue: continuity is being decided by an LLM guess instead of the explicit
signals we already have (the conversation thread and Telegram's reply-to chain).

## Design — deterministic, no classifier

Delete the relatedness classifier from the dispatch path. Replace `_pick_slot`
slot selection with three deterministic rules, in order:

```
inbound event (conversation C, optional reply_to_message_id M):
  1. reply & slot(M) known  → reuse slot(M)        [queue behind it if busy]
  2. else C's current slot is free → resume it      [sequential continuity]
  3. else a free slot exists → next progressive free slot   [new parallel lane]
  4. else → queue on slot 0                          [all busy]
  then: persist (this inbound message_id → chosen slot)
```

Rule semantics:

- **Rule 1 — explicit reply forces the original slot.** When the inbound is a
  reply (`reply_to_message_id` present in event meta) and we have a recorded
  slot for that original message, route to that exact slot. If the slot is busy,
  **queue behind it** (`should_queue = True`) — never start the reply on a
  different slot. This is the explicit-threading override.
- **Rule 2 — sequential continuity (the fix).** Non-reply message: resume the
  conversation's most-recent active slot **if it is free**. This keeps an
  ordinary back-and-forth on one slot → one session → full context. This is the
  rule that closes the fragmentation; rules 1 and 3 alone do not.
- **Rule 3 — new lane only on genuine overlap.** If the conversation's current
  slot is busy (a real concurrent burst), allocate the **next progressive free
  slot** (lowest free id — deterministic, replaces LRU). Parallelism spins up
  only when messages actually overlap, not on every message.
- **Rule 4 — all busy:** queue on slot 0 (unchanged).

"Current slot of C" = the slot of the most recently updated session row /
message-slot record for that conversation.

### Serial path unchanged

`max_concurrent <= 1` still short-circuits to `(0, False)`. All
`test_runtime_serial_unchanged.py` invariants must stay byte-identical. The
classifier deletion only affects the parallel path.

## Persistence — message → slot map

The slot that handled a message is currently in-memory (`_busy_slots`) + on the
session row; there is **no `message_id → slot` map**, so reply-to can't resolve
a slot today. Add one.

New table in `queue.db` (additive, `CREATE TABLE IF NOT EXISTS`, bump
`SCHEMA_VERSION`):

```sql
CREATE TABLE IF NOT EXISTS message_slots (
    channel          TEXT NOT NULL,
    conversation_id  TEXT NOT NULL,
    message_id       TEXT NOT NULL,   -- inbound source_message_id
    slot             INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (channel, message_id)
);
CREATE INDEX IF NOT EXISTS idx_message_slots_conv
ON message_slots(channel, conversation_id, created_at DESC);
```

- Written when an event is assigned a slot (in `dispatch_once` / `_pick_slot`
  caller), keyed on the inbound `source_message_id`.
- Read in rule 1: `slot_for_message(channel, reply_to_message_id)`.
- Read in rule 2: `latest_slot_for_conversation(channel, conversation_id)` — or
  derive from the sessions table's most-recent `updated_at` row for the
  conversation. Prefer one source of truth; sessions row is acceptable if its
  `updated_at` is reliably bumped per turn.
- Pruning: keep it bounded (e.g. delete rows older than N days or beyond the
  newest K per conversation) so it doesn't grow unbounded like the dedup index.

## Files

- `lib/gateway/runtime.py` — rewrite `_pick_slot`; delete
  `_classify_slot_affinity`, `_prefilter_slot_affinity`, `_slot_affinity_prompt`
  and their call sites/log lines (`slot_classifier_error`, classifier `slot
  pick` variants); persist message→slot after assignment; resolve reply-to from
  event meta (`reply_to_message_id`).
- `lib/gateway/queue.py` — `message_slots` table + schema bump +
  `record_message_slot`, `slot_for_message`, `latest_slot_for_conversation`,
  prune helper.
- `lib/gateway/sessions.py` — if rule 2 sources from sessions, expose a
  `latest_slot(channel, conversation_id)` helper.
- Tests:
  - `tests/gateway/test_pick_slot.py` — rewrite around the deterministic rules:
    reply→reuse (free + busy→queue), non-reply→resume current free slot,
    non-reply→new progressive slot when current busy, all-busy→slot 0,
    no-conversation→slot 0. No classifier mocking.
  - `tests/gateway/test_runtime_serial_unchanged.py` — must still pass untouched.
  - `tests/gateway/test_queue.py` — message_slots CRUD + prune + schema version.
  - Remove/replace classifier-specific tests.

## Out of scope

- Per-brain session split (a conversation that hops brains by triage intent
  still has separate session rows per brain). Tracked separately; this spec is
  slot routing only.
- Cross-channel threading. reply-to is Telegram-specific here; other channels
  fall through to rules 2–4 (no reply signal), which is correct.

## Rollout

No deploy in this PR. After merge it ships in the normal release; Livia's
`max_concurrent` is currently pinned to 1 as a stopgap — lift back to 8 once
this lands and is verified, so she regains parallelism *with* continuity.
