# Spec — JC audit Phase 1 "Fix-Now" package

- **Source:** fleet audit 2026-06-09 (fable-5), `rachel_zane/memory/L2/learnings/jc-audit-fable5-2026-06-09.md`
- **Branch:** `feat/jc-audit-phase1-fixnow`
- **Status:** awaiting review (Luca) — PR only, no merge, no deploy, no gateway restarts
- **Scope:** the four this-week findings. Explicitly NOT in scope: outbound idempotency
  ledger (feature 2), channel supervision, brain health probes, telegram hardening,
  config schema unification, doctor root-crontab checks (feature 9 beyond the install guard).

---

## Fix 1 — Recovery classifier repair (audit Finding 3, P1)

**Files:** `lib/gateway/recovery/classifier.py`, `tests/gateway/recovery/test_classifier.py`

### Root cause
1. `_parse_classifier_json` extracts the LLM reply with the non-greedy regex
   `\{[\s\S]*?\}`, which stops at the **first** `}`. The mandated reply schema ends with
   a nested object (`"extracted": {...}`), so every well-formed reply truncates to an
   unparseable prefix → `json.loads` fails → `None` → every LLM classification degrades
   to blind transient retry. `context_exhausted` / `context_profile_unavailable` are
   LLM-only by design, so they can never fire. The single existing unit test omitted
   `extracted` — the only reply shape that hides the bug.
2. Codex's real stale-session error — `thread/resume failed: no rollout found for
   thread id <uuid>` — matches no `session_missing` alternative in `_REGEX_RULES`, so the
   sticky session is never cleared (the Sofia 2026-06-05 endless-retry incident).

### Change
1. Replace the regex extraction with `json.JSONDecoder().raw_decode` scanning from each
   `{` candidate (same pattern already used by `lib/gateway/brain_output.py:122`). First
   candidate that decodes to a dict wins. Balanced nested objects parse correctly;
   leading prose / code fences are skipped.
2. Add `no rollout found for thread id|thread/resume failed` to the `session_missing`
   regex rule.

### Contract after
- A classifier reply containing one well-formed JSON object — with or without nested
  `extracted`, with or without surrounding prose — parses; `kind`/`confidence`/`extracted`
  flow through exactly as before. Replies with no decodable object still return `None`
  (caller falls back to `unknown`, source=`fallback` — unchanged).
- Codex stale-resume stderr classifies as `session_missing` at the regex prefilter
  (confidence 0.97, no LLM call), so recovery clears the sticky session and retries fresh.

---

## Fix 2 — Single crash-proof main loop + code-drift wired (audit Findings 1+4 of exec summary, P1)

**Files:** `lib/gateway/runtime.py`, `lib/gateway/queue.py`, `bin/jc-gateway`,
`tests/gateway/test_queue.py`, new `tests/gateway/test_run_forever.py`

### Root cause
- `GatewayRuntime.run_forever` (the loop that calls `_check_code_drift` →
  `SystemExit(42)` → watchdog respawn) has **zero callers**. `bin/jc-gateway cmd_run`
  runs its own diverged copy of the loop without the drift check. The fleet-wide
  assumption "local instances auto-upgrade via code-drift detection" has never been true
  (zero `code_drift_exit` log lines anywhere, ever).
- The `cmd_run` loop body is unguarded: `queue.connect`, `requeue_expired`, and the
  pre-try sections of `dispatch_once` can raise (sqlite `database is locked` under
  contention) and kill the daemon.
- `queue.requeue_expired` never increments `retry_count` (and `claim_next` nulls the
  `error` breadcrumb), so a poison event requeues forever: crash → watchdog respawn →
  re-claim → crash, with a ~`lease_seconds` period — the observed "shutdown loop every
  ~5 min" signature.

### Change
1. `run_forever` becomes the single production loop and absorbs everything `cmd_run`'s
   loop did, parameterized:
   `run_forever(*, poll_interval_seconds=None, reload_requested=None, on_tick=None)`.
   Per iteration, inside a `try/except Exception` guard: consume SIGHUP reload request →
   `on_tick()` (pidfile re-assert hook) → `_check_code_drift()` → requeue-expired tick →
   `dispatch_once()`. On exception: log with `kind="loop_error"`, exponential backoff
   (cap 60s), continue — the daemon no longer dies on a transient. `SystemExit`
   (code-drift 42, signals) propagates. `start_channels()`/`close()` stay in
   `run_forever` as today.
2. `cmd_run` keeps process concerns only (pidfile create/cleanup, signal handlers,
   startup log) and delegates the loop to
   `runtime.run_forever(poll_interval_seconds=args.interval_seconds, reload_requested=<consume RELOAD_REQUESTED>, on_tick=<re-write pidfile if missing>)`.
   It no longer calls `start_channels()`/`start_heartbeat()` itself (run_forever does;
   also removes the double `start_heartbeat` call).
3. `queue.requeue_expired(conn, *, now=None, max_retries=None)`: the requeue UPDATE now
   increments `retry_count`; when `max_retries` is given, rows whose incremented count
   exceeds it move to `status='failed'` (with `finished_at`, error preserved) in the same
   statement set. Returns the requeued ids as before; exhausted ids are reported via a
   second return value? — **No**: return type stays `list[int]` (requeued ids) to keep
   `claim_next`/`claim_batch` call sites untouched; the runtime tick logs exhausted rows
   by querying the delta (ids that expired but were not requeued).
   The runtime tick passes `max_retries=self.config.max_retries`; internal calls from
   `claim_next`/`claim_batch_same_conversation` keep `max_retries=None`
   (increment-only — claim paths must stay a short write txn).

### Contract after
- Exactly one dispatch loop exists. A `git pull` that touches `lib/**.py` causes
  `code_drift_exit` + `SystemExit(42)` within ~60s on every gateway, and the watchdog
  respawns it with fresh modules — the auto-upgrade contract becomes real.
- A transient exception in the loop body logs `loop_error` and backs off; it never kills
  the daemon. Real shutdown paths (SIGTERM/SIGINT via `stop_requested`, SystemExit) are
  unchanged.
- A poison event that repeatedly expires its lease accumulates `retry_count` and lands
  in `failed` after `max_retries` requeues instead of looping forever.

---

## Fix 3 — Per-claim lease token + delivery ownership gate (audit Finding 2 of exec summary / A-P1, P1)

**Files:** `lib/gateway/queue.py`, `lib/gateway/runtime.py`,
`tests/gateway/test_queue.py`, new `tests/gateway/test_claim_token.py`

### Root cause
`worker_id = f"gateway-{os.getpid()}"` is per-**process**. When a slot/dispatch thread
loses its lease (long task, DB contention) the event requeues and the *same process*
re-claims it — with the *same* `locked_by`. Every `status='running' AND locked_by=?`
guard (`complete`, `fail`, `renew_lease`, `reset_running_to_queued`) then passes for
**both** the stale thread and the fresh one. Delivery (`runtime.py:1802`) happens before
`complete()` and is never gated on ownership → both threads send → duplicate reply.
This survives the 31412a2 lease-heartbeat fix (it only made lease loss rarer).

### Change
1. **Per-claim token.** `claim_next` and `claim_batch_same_conversation` write
   `locked_by = f"{worker_id}#<uuid4().hex[:12]>"` — a fresh token per claim call (one
   token shared by a coalesced batch, which is claimed in one UPDATE). The returned
   `Event` rows carry it in `.locked_by`. The `worker_id=` parameter keeps its name; the
   prefix preserves operator-facing diagnostics (`gateway-<pid>#a1b2c3...`).
2. **Runtime threads the token, not the process id.** Everywhere `dispatch_once`,
   `_apply_goal_lifecycle`, `_dispatch_parallel`, `_run_in_slot` and the lease heartbeat
   used `self.worker_id` as `expected_locked_by`/`worker_id`, they now use the claim
   token from the claimed events (`events[0].locked_by`; `_bundle_events` already
   propagates it via `dataclasses.replace(latest, ...)`). A stale thread's
   complete/fail/renew now fails the guard (KeyError / renewed=0) even when the fresh
   claimant lives in the same process.
3. **Ownership gate before delivery.** New `queue.owned_count(conn, event_ids, locked_by)`
   (`SELECT COUNT(*) ... WHERE id IN (...) AND status='running' AND locked_by=?`).
   `process_event` checks it immediately before `_deliver_response` (and before the
   voice render) for the event's id + any `meta.coalesced_ids`. If **any** row is no
   longer owned, delivery is skipped with `kind="delivery_skipped_lease_lost"`; the
   brain response is still returned so the (now-failing) `complete()` path logs
   lease_lost as today. Conservative rule: when in doubt, the fresh claimant is the one
   that delivers. Events not claimed from the queue (`locked_by` empty — direct/legacy
   invocation paths, tests) skip the gate.

### Contract after
- At most one claimant can pass the pre-delivery ownership check for a given claim
  generation: the requeue clears `locked_by`, the fresh claim writes a *different*
  token, so the stale thread's gate query matches 0 rows.
- Supervisor compatibility: `lib/supervisor/runner.py` passes the *observed*
  `snap.event.locked_by` as `expected_locked_by` — works unchanged with tokens.
- Unguarded legacy calls (`expected_locked_by=None`, recovery integration) behave
  exactly as before. Not fixed here (P2, Phase 2): recovery `fail()` ownership,
  delivery-fallback double-send, outbound idempotency ledger.

---

## Fix 4 — euid/ownership guard on watchdog cron install (audit Finding 5 / E-P1)

**Files:** `lib/watchdog/install.py`, `tests/watchdog/test_install.py`

### Root cause
`watchdog.install.install()` has zero euid/ownership checks (grep: no `geteuid`/`st_uid`
anywhere in the install path). Invoked from a `root@host` SSH session it lands the
JC-WATCHDOG block in **root's** crontab: root cron respawns the gateway as root, the
codex/claude sandbox blocks `/home/<jc_user>/*`, state/logs become root-owned, and the
jc supervisor hits `PermissionError`. This contaminated 3 hosts on 2026-06-05
(katja_nyberg, sergio_gutierrez_rich, sergio_gutierrez_muscle); the only guard today is
operator memory (RULES.md / skill Phase 6).

### Change
`install()` (and `verify()`) call a new `_assert_invoker_owns_instance(instance_dir)`
first: if `os.geteuid()` exists and differs from `instance_dir.stat().st_uid`, raise
`RuntimeError` naming both uids and the correct invocation
(`su - <owner> -c 'jc watchdog install'`). Applies to dry-run too — a root dry-run
previews against root's crontab, which is equally misleading. If euid or owner cannot be
determined (non-POSIX, stat error), the guard is a no-op (fail-open: this guard targets
the root-contamination case, not platform portability).
`verify()` gets the same guard so a root-run `jc watchdog verify` / `jc doctor` can't
report "block missing" against the wrong user's crontab and tempt a root reinstall —
it fails loud with the ownership message instead.

### Contract after
- `jc watchdog install|verify` as the instance owner: unchanged.
- As root (or any other uid) on an instance owned by `jc_user`: refuses with an
  actionable error **before** reading or writing any crontab. The 2026-06-05
  contamination class becomes impossible through this code path.
- Out of scope (Phase 2, feature 9): doctor checks of root's crontab and gateway
  process uid; migrating bash-watchdog state out of /tmp.

---

## Verification plan

- `python3 -m pytest tests/gateway/recovery tests/gateway/test_queue.py tests/watchdog/test_install.py tests/gateway/test_code_drift.py` + new tests
  (`test_run_forever.py`, `test_claim_token.py`, classifier nested-extracted repro,
  requeue retry-count, euid guard).
- Full `python3 -m pytest tests/` to catch regressions.
- No live verification on the running fleet (would require gateway restarts — forbidden
  for this task). Called out in the PR body.
