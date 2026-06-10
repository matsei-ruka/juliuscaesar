# JC audit Phase 2 — fix-soon package

Audit: `jc-audit-fable5-2026-06-09` (fable-5). Stacked on Phase 1
(PR #90, `feat/jc-audit-phase1-fixnow`). Two findings.

## 1. Outbound idempotency ledger (audit Part 2 #1 + #2 — duplicate-reply root fix)

Phase 1 added per-claim lease tokens and a pre-delivery ownership gate
(`owned_count`). That stops a *stale* worker from sending after re-claim. It
does NOT stop the remaining duplicate windows:

- **Crash between send and `complete()`** — the reply went out, the row stays
  `running`, lease expires, fresh claim re-runs the brain and sends again.
  Survives process restarts; ownership gating can't see it (the fresh claimant
  legitimately owns the row).
- **Delivery-fallback double-send** (`delivery.py:31-56`) — a live-channel
  send that raises *after* Telegram accepted the request (read timeout) falls
  back to a second stateless send of the same response.

### Design

New `deliveries` table in `queue.db` (SCHEMA_VERSION 4 → 5, additive
`CREATE TABLE IF NOT EXISTS` — no destructive migration):

```sql
CREATE TABLE IF NOT EXISTS deliveries (
    event_id     INTEGER NOT NULL,
    channel      TEXT    NOT NULL,
    status       TEXT    NOT NULL,   -- 'sending' | 'sent'
    message_id   TEXT,
    locked_by    TEXT,               -- claim token of the attempt
    attempted_at TEXT    NOT NULL,
    sent_at      TEXT,
    PRIMARY KEY (event_id, channel)
);
```

`queue.py` API:

- `begin_delivery(conn, *, event_id, channel, locked_by) -> (verdict, message_id)`
  inside `BEGIN IMMEDIATE`:
  - no row → INSERT `sending` → `("proceed", None)`
  - row `sent` → `("already_sent", message_id)` — caller skips the send and
    completes the event with the prior message id.
  - row `sending`, same `locked_by` → `("proceed", None)` (same-claim re-entry)
  - row `sending`, different `locked_by` → `("ambiguous", None)` — a previous
    claim attempted a send and never confirmed nor cleared (crash mid-send or
    post-accept timeout). **At-most-once: skip the send, complete the event.**
    A possibly-lost reply beats a duplicate — duplicates are the fleet's #1
    recurring incident; a lost reply is one user re-ask.
- `finish_delivery(conn, *, event_id, channel, message_id)` — `sending → sent`.
- `clear_delivery(conn, *, event_id, channel, locked_by) -> bool` — delete a
  `sending` row owned by this claim after a **provably-undelivered** failure
  (send returned None = API-level definite no-send), so a retry may deliver.
- `delivery_record(conn, *, event_id, channel)` — read row (tests/doctor).

### Wiring (`runtime.py`)

New `_deliver_response_idempotent(event, channel, response, meta)` used by the
two event-keyed reply paths in `process_event` (deliver_only + main reply).
Events whose `locked_by` is not a claim token (direct calls, tests, notices,
slash replies) bypass the ledger and use plain `_deliver_response` — same
opt-in shape as Phase 1's `_delivery_ownership_ok`.

Sequence: `begin_delivery` → send → `finish_delivery` on message_id, else
`clear_delivery`. The ledger write after send is one sqlite INSERT-commit:
the duplicate window shrinks from "send → bookkeeping → complete (and any
restart in between)" to a single statement.

### Fallback gating (`delivery.py`)

New `strict_idempotency=False` kwarg on `deliver_response`. When True (set
only by the ledger-gated path):

- live send **returns None** → provably no message accepted → stateless
  fallback allowed (unchanged).
- live send **raises** → classify: pre-connect failures
  (`ConnectionRefusedError`, `socket.gaierror`, `URLError` wrapping them) are
  provably-undelivered → fallback allowed. Timeout-class and unknown
  exceptions are ambiguous → raise `DeliveryAmbiguous`; caller keeps the
  `sending` ledger row (blocks any future resend of this event/channel) and
  completes the event.

Default (False) keeps legacy behavior for every other call site.

### Out of scope (deliberate)

- Background-completion card sends (separate surface, not the incident class).
- Voice replies (`_render_voice_reply`) — text reply remains the ledger key.
- Ledger pruning — rows are tiny; prune with the existing dedup-row cleanup
  when that lands (audit A-P3).

## 2. getUpdates 409/429 regression (audit F-P1, regression from 42582e1)

`42582e1` made `http_json` return the parsed JSON body on HTTP 4xx instead of
raising (correct fix for the supervisor-card 400 dedup). Side effect: the
`getUpdates` poll loop (`telegram.py:1229`) never checks `data["ok"]`, so 409
(token bleed — the fleet's CRITICAL nuisance) and 429 now return
`{"ok":false}` instantly → zero-backoff tight poll loop, invisible in logs.
Before 42582e1 these raised and at least hit the generic 5 s sleep.

### Fix (in the poll loop — `_http.py` stays untouched, 400-body behavior kept)

After each `getUpdates` response: if `not data.get("ok")` →

- exponential backoff: `min(300, 5 * 2^(n-1))` seconds over consecutive
  not-ok polls; counter resets on the first ok response.
- 429: honor `parameters.retry_after` when larger than the computed backoff.
- 409: loud dedicated log (`kind=telegram_poll_conflict`) naming the
  cross-instance token-bleed hypothesis — this is the signal that was
  rendered invisible.
- sleep is interruptible (1 s slices checking `should_stop`) so shutdown
  isn't blocked for up to 300 s.

## Tests

- `tests/gateway/test_delivery_ledger.py` — begin/finish/clear semantics,
  already-sent skip, cross-claim ambiguity, clear-allows-retry, persistence
  across connections; runtime wrapper (duck-typed) send/skip/clear/ambiguous.
- `tests/gateway/test_delivery.py` — strict vs legacy fallback classification.
- `tests/gateway/test_telegram_poll_backoff.py` — 409 exponential backoff +
  conflict log, 429 retry_after honored, counter reset on ok, no tight loop.
