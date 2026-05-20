# Supervisor branch audit — opus

**Branch:** `feat/supervisor` (7 commits, +5197 lines)
**Auditor:** Claude Opus 4.7
**Date:** 2026-05-17
**Scope:** `lib/supervisor/`, `lib/gateway/queue.py` (new helpers), `lib/gateway/channels/_http.py`, `bin/jc-supervisor`, `tests/supervisor/`, `tests/gateway/test_queue.py`.

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 4 |
| High     | 8 |
| Medium   | 11 |
| Low      | 7 |
| Nit      | 4 |
| **Total** | **34** |

### Top 3 risks to fix before merging

1. **Bug #1 — Recovery counter resets every cycle (loop-guard violation).** `state.prune(active_ids)` removes the EventState as soon as the event leaves `running` (i.e. immediately after `apply_recovery` puts it back to `queued`, on the *next* tick). When the dispatcher re-claims and the supervisor sees it again, `recovery_attempts` starts at 0. A flapping adapter (segfault on every spawn) loops *forever* with no escalation — exactly the scenario Phase 6 was designed to stop.
2. **Bug #2 — `reset_running_to_queued` races against the dispatcher.** Supervisor's UPDATE only filters by `status='running'`. If the original lease has just expired and `claim_next` re-claims to a *new* worker between the snapshot read and the supervisor's UPDATE, the supervisor will reset the freshly-claimed event off the new worker, who then writes back `done` (since `complete()` has no status guard either — see #4). Two workers can end up running the same event with one stale completion.
3. **Bug #3 — `bin/jc-supervisor enable|disable` `sed`'s every `enabled:` key in `ops/gateway.yaml`.** The regex `s/^\(  *\)enabled: .*/\1enabled: $enabled/` rewrites *every* `enabled:` line — `supervisor.recovery.enabled`, `supervisor.channels.<x>: enabled`, and any other YAML block that uses the same key. A single `jc-supervisor disable` can silently flip recovery/channels off.

---

## Critical

### 1. Recovery counter resets after every recovery — escalation never fires for flapping events

- **Location:** `lib/supervisor/runner.py:183` (`state.prune(active_ids)`); interaction with `lib/supervisor/state.py:40-44` and `lib/supervisor/runner.py:90` (state.event(snap.event.id) creates a fresh EventState if missing).
- **Bug:** `active_ids` is built from snapshots, which only contain `status='running'` events. After `apply_recovery` flips the event to `queued`, the next tick's snapshot omits it → `prune` drops the EventState (and its `recovery_attempts`). When the dispatcher re-claims, the supervisor creates a new EventState with `recovery_attempts=0`. Result: an adapter that crashes deterministically on every spawn never hits `max_recovery_attempts`. Phase 6 is dead code in this scenario.
- **Repro:** Inject any deterministic crash (e.g. segfault on a specific input). Supervisor recovers → dispatcher re-spawns → adapter crashes → next tick recovers again → forever. `recovery_attempts` will oscillate 0→1→0→1.
- **Fix sketch:** Don't prune events with non-zero `recovery_attempts` (or `escalated=True`) until they've been observed `done`/`failed` for a configurable cool-down window. Alternative: persist a per-event recovery counter on the queue row itself.

### 2. Supervisor reset races against dispatcher re-claim — silent two-worker concurrency

- **Location:** `lib/gateway/queue.py:530-594` (`reset_running_to_queued`), interacting with `claim_next:281` and `complete:434` / `fail:456`.
- **Bug:** Supervisor reads `status='running'` then issues `UPDATE … WHERE id=? AND status='running'` outside any explicit transaction. Meanwhile `claim_next` runs `BEGIN IMMEDIATE`, `requeue_expired` (lease-expiry path), then claims. If the lease expired in the same window the supervisor decided to reset, the dispatcher may already have set `status='running'` again with a new `locked_by` — and the supervisor's UPDATE still matches because the status is still `running`. The original adapter (if it returns at all) and the new one both write back via `complete()` / `fail()`, which have **no status guard** (`UPDATE events SET status='done' WHERE id=?`). Net: the wrong response can overwrite the right one, or `started_at` gets wiped twice.
- **Repro:** (1) dispatcher claims event E to worker A; (2) worker A hangs > lease; (3) supervisor snapshots E with worker A's PID (dead); (4) `requeue_expired` fires inside `claim_next`, E → queued; (5) dispatcher re-claims E to worker B; (6) supervisor's reset UPDATE fires, status='running' is true, sets to queued and wipes `started_at`, `locked_by`; (7) dispatcher claims again to worker C; (8) worker A's deferred reply lands via `complete()` overwriting C's row.
- **Fix sketch:** Make `reset_running_to_queued` a CAS on `(status, locked_by, locked_until)` — supervisor must pass the `locked_by` it observed in the snapshot and only reset if unchanged. Add a status guard to `complete()` and `fail()` (`WHERE id=? AND status='running' AND locked_by=?`). Wrap `reset_running_to_queued` in `BEGIN IMMEDIATE`.

### 3. `jc-supervisor enable|disable` rewrites every `enabled:` key in `ops/gateway.yaml`

- **Location:** `bin/jc-supervisor:128`.
- **Bug:** `sed -i "s/^\(  *\)enabled: .*/\1enabled: $enabled/" "$YAML"`. The regex is unanchored to any block. With a config like
  ```yaml
  supervisor:
    enabled: true
    recovery:
      enabled: true
    channels:
      telegram:
        enabled: true
  ```
  `jc-supervisor disable` rewrites *all four* `enabled:` lines to `false`, silently killing recovery and channel routing.
- **Repro:** Add `recovery.enabled` and a `channels.telegram.enabled` to the YAML, run `jc-supervisor disable`, diff the file.
- **Fix sketch:** Use a YAML-aware editor (python with the same `_parse_yaml` round-trip) instead of `sed`. As a stopgap, replace `sed` with an `awk` block that tracks the `supervisor:` heading and only edits keys at indent depth 2 inside that block.

### 4. `complete()` and `fail()` have no status guard — stale worker can overwrite a freshly-claimed row

- **Location:** `lib/gateway/queue.py:434-453` (`complete`), `lib/gateway/queue.py:456-503` (`fail`).
- **Bug:** Both end with `UPDATE events SET status=… WHERE id=?`. No `AND status='running'`, no `AND locked_by=?`. Combined with bug #2 and with `requeue_expired` (which moves expired-lease running→queued inside `claim_next`), a delayed adapter result can clobber a row that has since been re-claimed, retried, or even escalated to `failed`. The supervisor's existence widens this window because it actively resets rows out from under workers.
- **Repro:** Force a worker to hang past its lease (e.g. `time.sleep(400)` in adapter). `requeue_expired` returns the event to queued; another worker claims and completes. Old worker eventually returns and overwrites with stale `response`.
- **Fix sketch:** Add `AND status='running' AND locked_by=?` to both updates; have callers pass the `locked_by` they expected; return False / raise if the row no longer matches.

---

## High

### 5. `_is_pid_alive` treats `PermissionError` as dead — false positives on foreign-user PIDs and PID recycling

- **Location:** `lib/supervisor/snapshot.py:142-147`.
- **Bug:** `os.kill(pid, 0)` raises `PermissionError` when the PID exists but belongs to a different user (post-recycling, container PID, or any cross-user scenario). The function returns `False` ("dead"), causing the supervisor to falsely declare a healthy adapter dead and reset its event. Wrong direction: PermissionError means the process EXISTS.
- **Repro:** Run supervisor as user A while adapter PID is recycled to a process owned by user B. `os.kill` raises PermissionError → recovery triggers → live (or recycled foreign) PID falsely treated as gone.
- **Fix sketch:** `except ProcessLookupError: return False` only. On `PermissionError`, return `True` (PID exists, just unprivileged). Additionally, cross-check PID's `/proc/<pid>/comm` or `/proc/<pid>/cmdline` to confirm it's actually the JC adapter before declaring alive.

### 6. PID recycling — supervisor can declare a dead event alive

- **Location:** `lib/supervisor/snapshot.py:179-188` (`_pid_map_from_log`) + `_is_pid_alive:142`.
- **Bug:** The PID is read from `gateway.log`. After the original adapter dies, the kernel can recycle that PID to any unrelated process (curl, ssh, etc.). `os.kill(pid, 0)` succeeds → `pid_alive=True` → `needs_recovery=False` → the event stays stuck in `running` forever.
- **Repro:** Adapter crashes leaving event=running. PID 1234 reused by an unrelated long-running process. Supervisor sees alive PID every tick, never recovers.
- **Fix sketch:** Compare `/proc/<pid>/cmdline` against the expected adapter binary (e.g. contains `claude`, `codex`, `pi`). If mismatch, treat as dead.

### 7. Stale gateway.log entries — old PIDs picked up across restart

- **Location:** `lib/supervisor/snapshot.py:179-188`.
- **Bug:** Only the last 200 lines of `gateway.log` are scanned. After gateway restart or log rotation, the spawn record can be older than the window. `pid_map.get(eid)` returns `None` → adapter.pid is None → `needs_recovery` returns False (it requires `pid is not None`) → events whose spawn line scrolled out are *invisible* to recovery forever.
- **Repro:** Long-running event spans a log-rotation event. Subsequent ticks never see its PID; if adapter dies, supervisor cannot recover.
- **Fix sketch:** Persist the spawn PID on the queue row itself (new column `adapter_pid` written by the dispatcher) instead of grepping the log.

### 8. `edit_card_slack` treats `message_not_found` as success — card permanently orphaned

- **Location:** `lib/supervisor/delivery.py:221`.
- **Bug:** `if "not_modified" in err or "message_not_found" in err: return True`. `message_not_found` means the message is GONE (user deleted, channel archived, message older than 24h with restricted scope). Returning True keeps `ev_state.channel_message_id` set, so every subsequent tick re-edits the missing ID — Slack returns the same error, supervisor reports success, and the user never sees a card again for that event.
- **Repro:** Send a card → user deletes it in Slack → next tick edits → 404 wrapped as `message_not_found` → state never resets → no further visible cards for this event.
- **Fix sketch:** Return `False` for `message_not_found` and have the runner clear `channel_message_id` so the next tick re-sends instead of editing.

### 9. Discord/Telegram edit 404 — no fallback path, card stuck forever

- **Location:** `lib/supervisor/delivery.py:266-292` (Discord edit), `lib/supervisor/runner.py:293-313` (edit branch).
- **Bug:** When edit fails (deleted message, expired window — Slack ~24h for some workspaces, Telegram has a 48h edit window for some message classes, Discord 404 on delete), the runner has no path to fall back to a fresh send. `ev_state.channel_message_id` is never cleared on edit failure; every subsequent tick edits → fails → silently logs.
- **Repro:** Tick 1: send card, ID stored. User deletes message. Tick 2: edit returns False. Tick 3: edit returns False. Forever.
- **Fix sketch:** On edit failure, null `ev_state.channel_message_id` so the next tick sends fresh. Add an "edit_failures" counter; after N, force a re-send.

### 10. Failed events leave their progress card open — no finalization for `status='failed'`

- **Location:** `lib/supervisor/runner.py:338-415` (`_finalize_completed`), comment at line 357 explicitly excludes `failed`.
- **Bug:** `_finalize_completed` only edits cards for events in `status='done'`. If the gateway's normal retry logic exhausts retries and the event lands in `failed` *without* going through supervisor escalation (e.g. a brain-level fatal classified by `RecoveryIntegration`), the card stays open with stale `📖 reading` text forever. Additionally, `state.prune(active_ids)` drops the EventState since the event is no longer running, so the message_id is lost — supervisor never gets another chance to finalize.
- **Repro:** Adapter returns a hard error → gateway runs `fail()` past `max_retries` → event=`failed`. Tick runs: not in snapshots → not in active_ids → pruned. Card stays open.
- **Fix sketch:** In `_finalize_completed`, branch on `status in ('done', 'failed', 'escalated')`. For non-done, edit to a neutral final card (no error detail, per loop-guard / no-crash-exposure spec).

### 11. Escalated events also leave their card open

- **Location:** `lib/supervisor/runner.py:121-144` (escalation path).
- **Bug:** When `escalate_to_failed` fires, the supervisor never edits the open card. Per spec the user should not see the crash, but the card showing `📖 reading` indefinitely is itself misleading. Then on the next tick the EventState is pruned (event left `running`), so the message_id is lost — no recovery path.
- **Repro:** Force 3 consecutive recoveries (or pre-set `recovery_attempts=2` in state.json, then dead-PID event). Card stays at last phase indefinitely.
- **Fix sketch:** Before calling `escalate_to_failed`, edit the open card to the final-card layout (✅ replaced by a neutral "completed/finished" without surfacing error). Drop `channel_message_id` after edit so it's not re-touched.

### 12. State.json read-modify-write race when two ticks overlap

- **Location:** `lib/supervisor/state.py:46-76` (load/save), `lib/supervisor/runner.py:58, 184` (load → save).
- **Bug:** Cron-driven ticks can overlap if a tick exceeds the cron interval (e.g. narrator HTTP timeouts of up to 12s × 5 calls = 60s, with default cron at 30s). Both ticks read the same state.json, both perform recoveries / increment counters, both save — last write wins. Recovery attempts can be undercounted (escalation never triggers) or duplicated (extra resets fire). The intra-tick throttle (`tick_interval_seconds`) only prevents the *second* tick from doing real work *if* the first already saved — but a long first tick won't have saved yet, and the second tick will pass the throttle check.
- **Repro:** Set narrator timeout to 30s, run two ticks 5s apart. Both load state.json with `last_tick_at=0`, both proceed. Both bump `narration_count`, only the later save persists.
- **Fix sketch:** Take an exclusive file lock (`fcntl.LOCK_EX` on a sidecar `state.lock`) for the duration of the tick. Or move per-event progress state to the queue.db row.

---

## Medium

### 13. `_PHASES_CACHE` module-level cache — never reloaded in long-running processes

- **Location:** `lib/supervisor/phases.py:12, 20`.
- **Bug:** `_PHASES_CACHE` is set once per process lifetime. Cron fires each tick as a new process, so in production this is moot — but a long-running test runner or any future in-process scheduler will never see edits to `phases.yaml`. Worse, if the cache is ever shared across tests (pytest-xdist with same worker), an `override_path` test could leak (it bypasses cache, but if the first call set the cache with the default and the second call asks for override, that's fine — actually OK).
- **Fix sketch:** Drop the cache or key by path mtime.

### 14. `_brain_map_from_log` matches non-spawn lines

- **Location:** `lib/supervisor/snapshot.py:166-176`.
- **Bug:** Unlike `_pid_map_from_log` (which gates on `"adapter spawn" in line`), the brain-map function scans every line that contains `event=` and `brain=`. Any dispatch/event log line with both tokens will overwrite the canonical spawn-time brain. Dispatcher logs reference brain on retry/fail lines.
- **Repro:** Event 10 spawns with brain=claude. Then a retry logs `event=10 retry brain=fallback model=…`. brain_map ends up pointing at the fallback even if the adapter is still running on claude.
- **Fix sketch:** Gate on `"adapter spawn" in line`, mirroring `_pid_map_from_log`.

### 15. `_read_gateway_log_tail` reads entire file

- **Location:** `lib/supervisor/snapshot.py:150-163`.
- **Bug:** Every tick reads the whole `gateway.log` into memory line-by-line before keeping the last 200. On instances with multi-GB log files, this is a 30s-cadence memory + I/O blast.
- **Fix sketch:** Seek to `max(0, size - 64KB)`, read tail, split lines, keep last 200.

### 16. Narrator banned-token check is case-sensitive substring only — easily bypassed

- **Location:** `lib/supervisor/narrator.py:91-95`.
- **Bug:** `_validate` lowercases output then checks `tok in lower`. Unicode lookalikes (`𝗀ateway`, full-width characters) bypass. Spacing/punctuation between letters bypass (`gate-way`). A jailbroken model could intentionally bypass. Also misses `JC`, `Caesar`, `instance`, `cron`, `recovery_patterns.yaml`, etc.
- **Fix sketch:** Normalize Unicode (NFKC, fold), strip non-alphanum, then check. Expand banned set. Run output through `redact_stderr` regex too for credential-shaped strings.

### 17. Narrator credential regex misses common formats

- **Location:** `lib/supervisor/narrator.py:36-38` (`_REDACT_RE`).
- **Bug:** Catches `api_key=`, `token:`, `secret:`, `authorization:`, `bearer …`. Misses:
  - AWS keys (`AKIA[A-Z0-9]{16}`)
  - Generic high-entropy tokens (`sk_live_…`, `xoxb-…`, `ghp_…`, `eyJ…` JWT prefix on its own line)
  - URL-embedded credentials (`https://user:pass@host`)
  - `password=`, `pwd=`, `passwd=`, `auth=`
  - Bearer tokens on a separate line from "Authorization"
- **Fix sketch:** Extend regex with the patterns above; consider redacting any 32+ char base64-ish run.

### 18. `apply_recovery` and `escalate_to_failed` open fresh connections — connection churn under load

- **Location:** `lib/supervisor/recovery.py:147, 181`; also `lib/supervisor/runner.py` calls `_fetch_statuses` which opens its own conn.
- **Bug:** Each call opens a new sqlite connection. For a tick with N recoveries + finalize + snapshot, that's N+2 connections per tick. Under contention each pays `busy_timeout=5000`. Better to pass one connection through the tick.
- **Fix sketch:** Open one connection in `run_tick`, pass to all helpers, close at the end.

### 19. `redact_stderr` applied to only last 2000 bytes — secrets earlier in tail leak

- **Location:** `lib/supervisor/narrator.py:174` (`tail_raw[-2000:]`).
- **Bug:** Tail is truncated *before* redaction, but if a credential appears in the first part of a long tail it never reaches the model anyway — that's fine. But the on-disk `_write_log` records `result.text` not the input tail. OK on input.
  More serious: `narrator_call` log records `result.text` only — but if the model echoes a partial credential and validate() lets it through (depending on regex coverage), the leak persists in the supervisor.jsonl log. Plus the input tail itself is *not* redacted in any persistent log; only the model sees the redacted version.
- **Fix sketch:** Log only metadata + final narration text *post-validation*; never log raw stderr tail.

### 20. `mark_event_failed` bypasses `retry_count` semantics

- **Location:** `lib/gateway/queue.py:597-623`.
- **Bug:** Goes straight from `running` → `failed` ignoring `retry_count`. Gateway's `fail()` increments retry_count and re-queues until `max_retries`. Supervisor's escalation skips this entirely, masking the gateway's own retry budget. If an operator inspects retry_count to gauge "how stuck was this event," they'll see N=0 but the event is failed.
- **Fix sketch:** Either bump retry_count to a sentinel or document explicitly that supervisor escalation is independent of gateway retry counting and exposes a distinct `error='recovery_escalated'` for filtering.

### 21. Telegram message_id "0" treated as truthy by python str — stored in state

- **Location:** `lib/supervisor/runner.py:311-313` + `_MultiChannelSender.send:578`.
- **Bug:** `_MultiChannelSender.send` returns `str(mid) if mid is not None else None`. If `send_card_telegram` returned 0 (shouldn't, but no guard), `str(0)="0"` is truthy. Stored in state. Subsequent edit calls convert via `_int_or_none(message_id) or 0` → `0` → falsy guard returns False. Stuck.
- **Fix sketch:** `return str(mid) if mid else None` (covers 0 and None).

### 22. `_finalize_completed` doesn't guard against state.events mutation during iteration

- **Location:** `lib/supervisor/runner.py:359-415`.
- **Bug:** First loop builds `candidate_ids` from `state.events.items()`. Second loop deletes via `del state.events[str(eid)]` while iterating `candidate_ids` — safe because candidate_ids is a separate list. *However* `state.events` is also touched implicitly by `state.event(snap.event.id)` in the recovery loop earlier. Order matters: finalize runs *before* recovery, so an event that finalize would dele could be re-added by recovery's `state.event()` call if the same ID is still active (shouldn't happen — done events not in active_ids). Edge case but not airtight.
- **Fix sketch:** Snapshot the keys once at the start of `_finalize_completed` and never mutate state.events from any other path during the loop.

### 23. `_decode_meta` and `meta.get('channel_id')` — type confusion on non-string meta values

- **Location:** `lib/supervisor/runner.py:505-511`, `lib/supervisor/snapshot.py:221-228`.
- **Bug:** Discord and Slack may put numeric IDs in meta. `_delivery_address` does `str(meta.get(…) or "")` — fine for int (`str(123) == "123"`) but if meta contains a `dict` or `list` under those keys (corruption), `str(...)` succeeds with garbage. No type validation.
- **Fix sketch:** Validate types: `v = meta.get(k); return str(v) if isinstance(v, (str, int)) else ""`.

---

## Low

### 24. `_parse_yaml` falls back to `_parse_simple_yaml` on PyYAML error — schema drift silently lost

- **Location:** `lib/watchdog/registry.py:15-22` (imported by `lib/supervisor/config.py:9` and `recovery.py:25`).
- **Bug:** When PyYAML is unavailable, the fallback simple parser may not handle complex YAML the user wrote. The supervisor silently falls back to defaults. No log line.
- **Fix sketch:** Log at debug level when the fallback path is taken.

### 25. `recovery_patterns.yaml` user input → `re.compile` without bound

- **Location:** `lib/supervisor/recovery.py:229-238`.
- **Bug:** Patterns are user-controlled. A malicious or accidental catastrophic-backtracking regex (`(a+)+b` matched against long stderr tails) burns supervisor CPU. Bad regexes are silently skipped on `re.error`, but not on slow regex.
- **Fix sketch:** Compile with a length-bounded wrapper; reject patterns deemed dangerous via a quick heuristic.

### 26. Italian language detection — 2-marker threshold is fragile

- **Location:** `lib/supervisor/snapshot.py:231-243`.
- **Bug:** Detects Italian if ≥2 of 12 substring markers (`" di "`, `" che "`, etc.) appear. English text with "di Maggio" or "il dolce" hits it. Short Italian replies miss it.
- **Fix sketch:** Trust `meta['language']` exclusively; otherwise default to en. Or use a lightweight language detector library.

### 27. `_LABELS["ago_seconds"]["it"]` uses `s` not `secondi` — terse style mismatch

- **Location:** `lib/supervisor/cards.py:34-37`.
- **Bug:** English uses "8s ago" (terse), Italian uses "8s fa". Consistent, but Italian users may expect "8 secondi fa" / "8s fa" — minor i18n inconsistency.
- **Fix sketch:** Cosmetic only.

### 28. CLI `reset` doesn't acknowledge non-numeric event_id

- **Location:** `bin/jc-supervisor:147`.
- **Bug:** `int(sys.argv[2])` raises `ValueError` for non-numeric input, producing a Python traceback at the user. Should be a friendly error.
- **Fix sketch:** Try/except and print usage.

### 29. `jc-supervisor status` returns 0 even when supervisor disabled

- **Location:** `bin/jc-supervisor:84-121`.
- **Bug:** No exit code variation. Consumers (cron monitoring) can't distinguish "supervisor disabled" from "supervisor healthy".
- **Fix sketch:** Exit 2 (or some sentinel) when disabled.

### 30. `_write_log` swallows `OSError` silently

- **Location:** `lib/supervisor/runner.py:495-502`.
- **Bug:** If `state/logs/supervisor.jsonl` can't be written (disk full, permission), failures are invisible. No fallback to stderr.
- **Fix sketch:** Print to stderr on OSError so cron emails the error.

---

## Nit

### 31. Duplicate channel-defaults dict in `config.py`

- **Location:** `lib/supervisor/config.py:42-51` (SupervisorConfig default) and `79-87` (parser default).
- **Bug:** Two copies of the same dict must stay in sync. DRY violation.
- **Fix sketch:** Extract to a module constant.

### 32. `runner._int_or_none` defined locally — duplicates pattern elsewhere

- **Location:** `lib/supervisor/runner.py:488-492`.
- **Fix sketch:** Probably fine to leave; one-liner.

### 33. `NarratorResult` and `RecoveryDecision` dataclasses both `frozen=True` but mutated via runner

- **Location:** runner mutates `EventState` (not these), which is the right choice. Comment is for clarity only.

### 34. Spec doc dated 2026-05-17 with status "Proposed"

- **Location:** `docs/specs/supervisor.md:3`.
- **Bug:** Spec status should be updated to "Implemented" or similar as part of the merge.

---

## Test gaps

The test suite is solid for happy paths and basic edge cases. Gaps:

- **No concurrent-tick test.** Bug #12 is invisible to single-process tests.
- **No race test for `reset_running_to_queued` vs `claim_next`.** Bug #2.
- **No assertion that `recovery_attempts` persists across a recovery→requeue→reclaim cycle.** Bug #1: a test that runs 3 ticks across a flapping event would catch this.
- **No test for foreign-user PID / PID recycling.** Bug #5, #6.
- **No test for `_brain_map_from_log` confusing retry lines with spawn lines.** Bug #14.
- **No test asserting `complete()` / `fail()` refuse to write to a non-running row.** Bug #4.
- **No test for card finalization on `status='failed'`.** Bug #10.
- **No test for `edit_card_slack` on `message_not_found` actually resulting in a re-send.** Bug #8.
- **`test_dead_pid_triggers_silent_recovery` uses PID 999999** — flaky on hosts where that PID exists.
- **No test for `jc-supervisor enable|disable` clobbering other `enabled:` keys.** Bug #3.
- **`_PHASES_CACHE` module-level state can leak between tests if any test runs without `override_path`.** No explicit reset fixture.

---

AUDIT COMPLETE — 34 bugs found, see docs/specs/supervisor-audit-opus.md
