# JC audit Phase 3 — last duplicate source + poison escalation + rollout plan

Audit: `jc-audit-fable5-2026-06-09` (fable-5). Stacked on Phase 2
(PR #91, `feat/jc-audit-phase2-fixsoon`, itself stacked on merged PR #90).
Two code findings + the fleet rollout plan for the whole audit stack.

## 1. opencode stale-reply fallback (audit C-P2, `adapters/opencode.sh:110-124`)

The last duplicate-reply source left alive after Phases 1–2. Phase 1 gated
delivery on claim-token ownership (queue side); Phase 2 added the outbound
idempotency ledger (delivery side). Both assume the *brain returned a fresh
reply*. The opencode adapter can violate that assumption:

opencode 1.16 under `--format json` emits no text on stdout — the adapter
reads the reply back from `~/.local/share/opencode/opencode.db` with

```sql
SELECT id, data FROM message
WHERE session_id=? AND json_extract(data,'$.role')='assistant'
ORDER BY time_created DESC LIMIT 1
```

There is **no freshness floor**. On a resumed session (`--session $RESUME`),
if `opencode run` exits rc=0 but writes no new assistant row (model produced
a tool-only turn, opencode internal error swallowed, store write raced), the
query returns the **previous turn's reply** and the adapter emits it as the
fresh response. The gateway then delivers it: a duplicate from the ledger's
point of view it is a *new* event with new content — neither Phase 1 nor
Phase 2 can catch it. Adapter-level bug needs an adapter-level fix.

### Design — pre-run snapshot, id-equality guard

A wall-clock floor (`time_created >= run start`) is fragile: the column's
unit (s vs ms epoch) is an opencode implementation detail, and clock skew
between host and store writes is unbounded. Instead, snapshot the store
*before* the run and reject non-progress:

1. **Pre-run** (only when `$RESUME` is set and the DB exists): fetch the id
   of the latest assistant message for the resume session — `PREV_MSG_ID`.
   Snapshot failure (DB locked/missing) → empty snapshot → legacy behavior,
   with a stderr note.
2. **Post-run**: run the existing latest-assistant query. If the returned
   `id == PREV_MSG_ID` **and** the queried session is the resume session,
   no new assistant row exists → the DB "reply" is the previous turn.
   Treat as *no DB reply*: fall through to the NDJSON text-event fallback
   (inherently scoped to this run), write `error: "stale reply suppressed —
   no new assistant message after run"` into the usage sidecar, log to
   stderr.
3. If the NDJSON fallback is also empty, the adapter emits **empty stdout,
   rc=0**. The runtime skips delivery of empty responses (`runtime.py:535`)
   and completes the event — consistent with the Phase 2 at-most-once design
   call: a possibly-lost reply beats a duplicate. Exiting non-zero instead
   would re-run the whole turn through recovery and double-execute any tool
   side effects the turn already performed.

Guard applies only when the post-run session id equals the resume session:
if opencode silently opened a *new* session, the snapshot is from a different
session and must not suppress its reply.

Fresh sessions (no `$RESUME`) are untouched — no prior turn exists in the
session, so the stale path cannot fire.

### Files

- `lib/heartbeat/adapters/opencode.sh` — pre-run snapshot (python3 one-shot,
  same read-only `mode=ro` URI as the post-run query); pass `PREV_MSG_ID`
  into the existing post-run heredoc; equality guard + sidecar error.
- `tests/gateway/test_opencode_brain.py` — extend the existing
  `OpencodeAdapterEndToEnd` harness (stub `opencode` + real SQLite store):
  - resumed session, run writes no new assistant row → stdout empty, sidecar
    carries the stale-suppression error, previous reply NOT emitted;
  - resumed session, run writes a new assistant row → new reply emitted
    (guard does not over-suppress);
  - fresh session (no resume) → unchanged behavior.

## 2. Poison-event escalation on the claim path (audit A-P2 completion)

Phase 1 (`1893eb6`) taught `requeue_expired` to increment `retry_count` and
route rows past `max_retries` to `failed` — but only the runtime's
`_requeue_expired_tick` passes `max_retries`. The other two callers run
*inside the claim transaction*:

- `claim_next` (`queue.py:393`) → `requeue_expired(conn, now=now)`
- `claim_batch_same_conversation` (`queue.py:456`) → same

Both increment but never escalate. The claim-path requeue runs in the same
`BEGIN IMMEDIATE` as the claim SELECT, so an expired poison row is flipped
back to `queued` and **re-claimed in the same transaction** — it never
survives to the runtime tick where escalation lives. Net effect: poison
events still loop forever; `retry_count` grows unbounded but nothing reads
it on this path. The Phase 1 fix is wired into the wrong half of the race.

### Design — thread `max_retries` through the claim functions

- `claim_next(conn, *, worker_id, lease_seconds=300, sources=None,
  max_retries=None)` — forward to the inline `requeue_expired`.
- `claim_batch_same_conversation(...)` — same.
- `runtime.dispatch_once` — pass `max_retries=self.config.max_retries` at
  all three claim sites (parallel single, coalesced batch, serial single).
- `bin/jc-gateway` `cmd_claim` / `cmd_work_once` (operator debug commands)
  keep `max_retries=None` — legacy increment-only behavior, documented here.
  Debug claims should inspect, not change failure-routing semantics.

No schema change, no signature break (keyword with default `None`).
`requeue_expired` itself is untouched — Phase 1 semantics (`failed` rows get
`error='lease expired (max retries exceeded)'`, excluded from the return
value) carry over verbatim.

### Files

- `lib/gateway/queue.py` — the two claim functions.
- `lib/gateway/runtime.py` — `dispatch_once` claim sites.
- `tests/gateway/test_queue.py` — poison row past `max_retries` is routed to
  `failed` by `claim_next` / `claim_batch_same_conversation` and is NOT
  claimed; row below the cap is requeued and remains claimable.
- `tests/gateway/test_runtime_serial_unchanged.py` or equivalent — config
  `max_retries` reaches the claim path (one integration-shaped assertion).

## 3. Fleet rollout plan — audit stack (post-#91 + #92 merge)

Scope: PR #90 (merged, NOT deployed), #91 (open), this PR (#92, stacked on
#91). Nothing below executes in this PR — this is the operator runbook.

### What the stack changes operationally

| Change | Operational impact |
|---|---|
| Per-claim lease tokens (#90) | `locked_by` becomes a token; in-flight old-style rows requeue once on lease expiry, then carry tokens. No migration. |
| Crash-proof loop + code-drift wiring (#90) | Gateways now self-exit(42) on framework code drift → watchdog respawns on new code. First deploy after this lands makes *future* deploys self-applying for local instances. |
| Poison escalation (#90 + this PR) | Events exceeding `max_retries` (default 3) land in `failed` instead of looping. Watch `failed` counts in the first 48h — pre-existing poison rows will surface immediately. |
| euid guard (#90) | `jc watchdog install/verify` refuses as wrong user. Fleet installs via `root@` SSH MUST already use `su - <jc_user> -c` (standing rule) — now enforced. |
| deliveries table (#91) | queue.db SCHEMA_VERSION 4→5, additive. **Gateway restart required after the schema-touching deploy** (old process holds stale code; known rule). `jc update` handles this. |
| getUpdates backoff (#91) | 409 token-bleed becomes a loud log line — grep for it fleet-wide after deploy; it was previously invisible. |
| opencode stale-guard (this PR) | opencode instances may show occasional "stale reply suppressed" sidecar errors — that is the bug being caught, not a regression. |

### Order of operations

1. **Merge order:** #91 → then #92 retargets to `main` automatically (or
   rebase if GitHub doesn't auto-retarget the stack) → merge #92. Never
   deploy mid-stack: #91's ledger assumes #90's claim tokens are present.
2. **Tag a release** (`v2026.06.XX.1`) after the stack is fully merged.
   Single release for the whole stack — partial deploys mix lease-token
   writers with non-token readers.
3. **Canary (24h):** one non-critical instance — suggest
   `sergio_gutierrez_muscle` (VM 120, stable, low traffic, no principal
   depending on it). Deploy via `jc update --yes` (release deploys MUST use
   jc update — release hooks; raw git pull skips them). Then:
   - `jc doctor` → zero errors (mandatory close-out for any config/restart).
   - `sqlite3 state/queue/queue.db "PRAGMA user_version"` → 5 equivalent /
     `deliveries` table present.
   - Send 3 test messages incl. one long-running (>300s lease) task →
     exactly one reply each.
   - `grep -c "409" state/gateway/logs/*` → confirm no token-bleed noise.
4. **Fleet wave (after clean canary):** all remote hosts via `jc update
   --yes`, per-host `jc doctor` after each. Use the SSH user map (`.242` is
   root@; most others jc@/lucamattei@ — check map, don't default). Known
   trap: jc-update can kill the gateway on shared-process-group instances
   (Sophie .105, Daniel .104, Rafael .209) — run `jc-gateway --instance-dir
   <inst> restart` after jc-update on those three, then doctor.
5. **Local .246 instances last, rachel_zane self EXCLUDED.** Never restart
   own gateway mid-task (standing rule) — after this deploy lands on disk,
   rachel's gateway picks it up via the now-actually-wired code-drift
   self-exit, or Luca restarts externally. Sibling local instances: `env -i`
   discipline on any manual start (token-bleed nuisance) until the env
   allowlist phase ships.
6. **48h watch:** `failed` queue counts (surfaced poison rows — triage each:
   genuine poison → delete; transient-storm victims → `jc-gateway retry`),
   duplicate-reply reports (expect zero), "stale reply suppressed" sidecar
   errors on opencode instances, 409 log lines.

### Rollback

`jc update` to the previous tag + gateway restart. Schema v5 is additive —
old code ignores the `deliveries` table, no down-migration needed. Lease-token
rows degrade gracefully (old code treats token as an unknown worker id →
requeue on expiry, one extra retry, no loss).

### Explicitly out of scope for this rollout

Phase 4+ audit items: channel supervision, brain health probes, config
schema centralization, env allowlisting end-to-end, telegram outbound
hardening pack (4096 chunking et al.), ownership-aware doctor checks.
