# JC audit Phase 4 — remaining features (channel supervision, brain health, telegram hardening, config schema, env allowlist, liveness)

Audit: `jc-audit-fable5-2026-06-09` (fable-5). Stacked on merged Phases 1–3
(PRs #90/#91/#92). One consolidated PR. NOT merged, NOT deployed.

## Gap analysis — audit vs. Phases 1–3 (verified against `origin/main` @ 56e2ec7)

### Executive-summary findings

| # | Finding | Status | Where |
|---|---------|--------|-------|
| 1 | Code-drift self-restart never runs | **DONE** | `1893eb6` — `run_forever` is the single loop, `_check_code_drift` wired (runtime.py:607) |
| 2 | Duplicate-reply mechanism alive | **DONE** | `97235fd` (claim tokens + ownership gate), `406bb6a` (delivery ledger), `b596c7b` (opencode stale guard), `43b8453` (claim-path poison escalation) |
| 3 | Recovery classifier broken by design | **DONE** | `b46fcfa` — raw_decode parse + codex `no rollout found` regex |
| 4 | Main dispatch loop unguarded | **DONE** | `1893eb6` — exception guard + backoff, requeue counts retries |
| 5 | Cross-instance token bleed half-fixed | **MISSING → this PR** | 4 residual `os.environ`-first sites + `brains/base.py:366` full-env copy (Feature 8) |

### Ten recommended features

| # | Feature | Status | Where / what's left |
|---|---------|--------|---------------------|
| 1 | Per-claim lease tokens + delivery gating | **DONE** | `97235fd` (PR #90) |
| 2 | Outbound idempotency ledger | **DONE** | `406bb6a` (PR #91) |
| 3 | Single crash-proof main loop | **DONE** | `1893eb6` (PR #90) |
| 4 | Channel supervision | **MISSING → this PR (safe slice)** | constructor isolation + supervised threads + health surfaced; SIGHUP-live-rebind deferred |
| 5 | Brain health probes + fallback validation | **MISSING → this PR** | static probes (doctor + startup), BrainFailureStore TTL + clear-on-recovery; live trivial-invoke probe deferred |
| 6 | Telegram outbound hardening | **PARTIAL → this PR completes** | done: getUpdates ok-check + 409/429 backoff (`a5996a0`). Left: 4096 chunking, 429 retry on sends, offset-advance-after-processing, send_photo/editMessageText response checks, voice-caption truncation order |
| 7 | Config schema single source of truth | **MISSING → this PR (scoped slice)** | kill naive-YAML silent fallback, validate nested triage values + supervisor section, preserve explicit zeros, fix `triage_cache_ttl_seconds` drift, route `jc-supervisor enable/disable` through validated atomic writer. Full one-schema-object refactor deferred (stub below) |
| 8 | Env allowlisting end-to-end | **MISSING → this PR** | purge 4 `os.environ`-first sites, adapter subprocess env allowlist, `.env`-only resolution for secret keys, `sanitize()` filters dotenv through reserved-key predicate |
| 9 | Ownership-aware install + doctor | **PARTIAL → this PR completes doctor side** | done: euid guard on install/verify (`d02318d`). Left: doctor checks for gateway process uid, state/ ownership, root-crontab JC-WATCHDOG. Bash-watchdog `/tmp` state move deferred (touches live watchdog behavior fleet-wide) |
| 10 | Real liveness + fleet observability | **MISSING → this PR (safe slice)** | heartbeat tied to dispatch progress (loop ticks + lease renewals), `jc doctor --json` with queue metrics. fleet-health skill consumption is instance-repo work, out of framework scope |

Part 1 subsystem findings not covered by features 1–10 (triage parser salvage,
gemini yolo inversion, escaper fence ordering, brain_failure no-TTL [picked up
in Feature 5], etc.) remain backlog — tracked in the audit doc, not silently
dropped. This PR ships the feature list only.

---

## Feature 4 — channel supervision (safe slice)

**Files:** `lib/gateway/channels/registry.py`, `lib/gateway/channel_lifecycle.py`,
new `tests/gateway/test_channel_supervision.py`

### Today
- `build_enabled_channels` (registry.py:42-47): one channel constructor raising
  kills the gateway at boot.
- `ChannelLifecycle.start` (channel_lifecycle.py:37-47): plain daemon threads;
  a channel thread crashing (or its `run()` returning mid-flight) is silent
  forever — gateway heartbeat stays fresh, watchdog sees healthy, Telegram
  inbound is dark.

### Change
1. **Constructor isolation.** New `enabled_channel_factories(instance_dir,
   config, log) -> dict[str, Callable[[], Channel]]` in registry.py — returns a
   zero-arg rebuild closure per enabled channel. `build_enabled_channels` stays
   (compat) but now wraps each factory call in try/except: a raising constructor
   logs `channel build failed name=<n>` and is skipped; the others still boot.
2. **Supervised runner threads.** `ChannelLifecycle.start` spawns one
   `_supervise(name, factory)` thread per enabled channel:
   - run the channel; on **exception**: log `channel crashed`, restart with
     exponential backoff (5s → cap 300s; reset to 5s after a run that lasted
     ≥300s). The channel instance is **rebuilt via the factory** before each
     restart (a crashed poller may hold broken sockets/conns).
   - on **clean return**: if `stop_requested()` → exit. If the first run
     returned in <60s → deliberate no-op (e.g. telegram "token missing") → mark
     `not-ready`, do NOT restart-loop it. If a run lasted ≥60s and returned
     without stop → treat like a crash (long-pollers don't return mid-flight);
     restart with backoff.
   - backoff sleep is interruptible (1s slices checking `stop_requested`).
3. **Health surfaced.** `ChannelLifecycle.health_snapshot() -> dict` —
   per channel: `state` (`running|not-ready|backoff|stopped`), `restarts`,
   `last_error`, `last_exit_at`. Written to
   `state/gateway/channel_health.json` (atomic, best-effort) on every state
   transition; `jc doctor --json` (Feature 10) includes it.

### Deferred (follow-up spec stub)
Live config rebind on SIGHUP (running channels keep constructor-captured cfg —
audit B-P3) and per-channel watchdog escalation policy. Needs design around
channel-specific reload semantics; not safe to bolt on here.

## Feature 5 — brain health probes + fallback validation

**Files:** new `lib/gateway/brain_health.py`, `lib/gateway/brain_failure.py`,
`lib/gateway/runtime.py`, `bin/jc-doctor`, new `tests/gateway/test_brain_health.py`

### Today
A fallback brain is validated by name prefix only (config.py): `pi:minimax-m3`
with no pi binary/auth passes config + doctor and fails at runtime as a 300s
hang (the .209 incident). `BrainFailureStore.mark_failed` is permanent — no
TTL, no clear-on-recovery call site; one auth blip diverts routing forever.

### Change
1. **`brain_health.probe_spec(instance_dir, spec, role) -> ProbeResult`** —
   static checks, no live invoke:
   - spec parses + brain is in the dispatch registry;
   - adapter script exists + executable (delegates to `Brain.validate()`,
     which openrouter extends with an API-key check);
   - CLI binary on PATH (brain→binary map: claude/codex/opencode/gemini/
     aider/pi/grok; API-class brains skip);
   - auth artifact hint (warn-level): claude `~/.claude/.credentials.json`,
     codex `~/.codex/auth.json`, pi `~/.pi/agent/auth.json`,
     openrouter `OPENROUTER_API_KEY` in `.env`.
   `probe_all(instance_dir, cfg)` collects every configured spec with its role:
   `default_brain`, per-channel defaults, `triage.fallback_brain`
   (default_fallback_brain), `triage.unsafe_fallback_brain`, `triage_backup`
   values, supervisor `narrator_brain` + `recovery.fallback_brain`.
2. **Doctor section.** `jc doctor` gains "Brain health": FAIL when the default
   brain or any *fallback-role* spec fails (fallbacks MUST work — they run
   exactly when the primary is broken), WARN otherwise.
3. **Startup validation.** `run_forever` probes fallback-role specs once at
   start; failures log `kind="brain_probe_failed"` loudly. Boot proceeds
   (a broken fallback must not take down the primary path).
4. **`BrainFailureStore` TTL + recovery.** `is_failed(brain)` expires entries
   older than `ttl_seconds` (default 6h, constructor kwarg) — expired entries
   are dropped and persisted. Runtime clears the entry after every successful
   brain invocation (clear-on-recovery) — one auth blip no longer diverts
   routing forever.

### Deferred
Live trivial-invoke probe (`--probe-brains`): real CLI invocation is slow
(seconds per brain) and can consume metered quota; needs operator opt-in
design. Route-time `is_failed()` consult on the fallback path (audit D-P1
second half) rides on the TTL fix; full re-route logic deferred.

## Feature 6 — Telegram outbound hardening (remainder)

**Files:** `lib/gateway/channels/telegram_outbound.py`,
`lib/gateway/channels/telegram.py`, `lib/gateway/runtime.py`,
new `tests/gateway/test_telegram_outbound_hardening.py`

1. **4096 chunked sends** (`telegram_outbound.send_text`). New
   `split_for_telegram(text, limit=4096)`: splits the *raw* text on paragraph
   boundaries (fence-aware — never splits inside a code fence; an oversize
   fence block is split by lines and re-wrapped with its opening fence +
   closing fence per chunk), greedily packing while the **escaped** chunk fits
   the limit; a single oversize line hard-splits at `limit//2` raw chars
   (escaping at most doubles length). `send_text` sends every chunk in order;
   `reply_to_message_id` only on the first; per-chunk parse-error fallback as
   today; returns the **last** chunk's message_id (the footer, appended
   upstream of the slice, now survives in the final chunk instead of being
   cut first).
2. **429 retry on sends.** Shared `_post_with_retry(url, payload, timeout,
   log)` in telegram_outbound: on `ok=false, error_code=429` honors
   `parameters.retry_after` (capped 60s), max 3 attempts, then raises as
   today. Used by `send_text` (both normal and parse-fallback posts).
3. **Offset advance survives enqueue failure** (telegram.py poll loop). The
   per-update `enqueue(...)` is wrapped: on exception the channel logs
   `kind=telegram_enqueue_failed`, **rewinds `self.offset` to this update_id**
   and breaks the batch — the message is re-fetched next poll instead of
   dropped (the transient-sqlite-locked loss). Poison guard: 3 consecutive
   failures for the same update_id → advance past it with a loud drop log
   (a poison update must not wedge inbound forever). Media-ingestion failures
   keep their current deliberate skip-with-log semantics.
4. **send_photo response check.** `send_photo` gains `log` kwarg; API
   not-ok / transport errors are logged with the response body. The runtime
   caller logs "image sent" only when a message_id came back; otherwise
   `image send failed (api)`. (No `sendDocument` call site exists in the
   framework today — checked; skills hit Bot API directly.)
5. **editMessageText response check** (`telegram.py:_edit_message_text`):
   inspect `data["ok"]`; log description on failure (approval cards keeping
   live buttons after the decision was applied).
6. **Voice caption truncation order** (`send_voice`): truncate the raw
   caption first, then escape; shrink until the escaped form fits 1024 —
   truncating after escaping could cut an escape pair mid-entity → 400 →
   voice message lost.

## Feature 7 — config schema single source of truth (scoped slice)

**Files:** `lib/gateway/config.py`, `lib/gateway/config_writer.py`,
`bin/jc-supervisor`, `tests/gateway/test_config_env.py` (+ new cases)

The full feature (one schema object consumed by validator, loaders, and every
writer tool) is **L-effort and deferred** — stub at
`docs/specs/jc-config-schema-unification.md`. This slice kills the active
incident classes:

1. **No silent YAML fallback.** `config._load_raw` and
   `config_writer._load_yaml_text` currently `except Exception` around
   `yaml.safe_load` → a real YAML syntax error silently degrades to the naive
   line parser, which has different semantics (drops lines) → the validator
   validates a DIFFERENT config than the operator wrote. Change: fall back to
   `_parse_simple_yaml` **only on `ImportError`** (PyYAML genuinely absent);
   `yaml.YAMLError` raises `ConfigError` naming file + parse error.
2. **Nested triage values validated.** The validator's value checks read only
   top-level keys; nested `triage:` equivalents escape them (threshold 7
   loads → everything routes to fallback forever). Validate via the same
   top-level-then-nested resolution the loader uses:
   `triage_confidence_threshold` ∈ [0,1] and numeric,
   `default_fallback_brain` is a known brain spec,
   `triage_cache_ttl_seconds` / `sticky_brain_idle_timeout_seconds`
   non-negative ints.
3. **`triage_cache_ttl_seconds` drift fix.** Loader reads it top-level;
   validator rejects it top-level (missing from `allowed_top`). Add to
   `allowed_top` — writer/validator drift, same family as the `supervisor:`
   incident.
4. **Supervisor section contents validated.** `supervisor:` is accepted as a
   key but validated by nobody. Minimal checks: `enabled` bool-like,
   `narrator_brain` + `recovery.fallback_brain` (when non-empty) parse as
   known brain specs (narrator additionally allows `openrouter`),
   unknown supervisor keys warn-listed in errors.
5. **Explicit zeros preserved.** `or`-defaulting destroys valid zeros:
   `reliability.log_backups: 0` → 5, `gateway.max_retries: 0` → 3 (operator
   disabling retries gets triple-fire). Replace with None-checks.
6. **`jc-supervisor enable/disable` through the validated writer.** New
   `config_writer.set_supervisor_enabled(instance_dir, enabled) -> bool`:
   load → mutate → dump → **validate the new raw dict** (`_validate_raw_config`)
   → atomic write; on validation failure nothing is written and the error
   propagates. `bin/jc-supervisor` calls it instead of its own
   `yaml.safe_dump` + bare `write_text` (non-atomic, unvalidated — the
   incident-2 pattern).

## Feature 8 — env allowlisting end-to-end

**Files:** `lib/gateway/config.py`, `lib/gateway/brains/base.py`,
`lib/gateway/env_isolation.py`, `bin/jc-gateway`,
`lib/gateway/channels/email_dispatcher.py`, `lib/commitments/actions.py`,
`lib/gateway/brains/openrouter.py`, `lib/heartbeat/lib/send_telegram.py`,
`tests/gateway/test_env_isolation.py` (+ new cases)

Closes the cross-instance impersonation chain (CRITICAL known nuisance)
instead of relying on `env -i` operator discipline.

1. **Secret keys never resolve from the parent process env.**
   `config.env_value` gains a `_SECRET_ENV_KEYS` set (`TELEGRAM_BOT_TOKEN`,
   `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
   `DASHSCOPE_API_KEY`, `COMPANY_API_KEY`, `MINIMAX_API_KEY`): for these,
   resolution is `.env`-only — absent means empty, never the sibling shell's
   token (fail-loud at the call sites, which already log "no
   TELEGRAM_BOT_TOKEN"). Non-secret keys keep the os.environ fallback
   (compat).
2. **Purge the four `os.environ`-first sites** (audit G-P1):
   - `channels/email_dispatcher.py:255` — token via `env_value` only; drop
     the bare `os.environ.get("TELEGRAM_CHAT_ID")` rung (sibling misroute);
     `TELEGRAM_CHAT_ID_OVERRIDE` env stays (intentional per-invocation
     routing, set by the gateway itself).
   - `lib/commitments/actions.py:34` — same.
   - `brains/openrouter.py:31,54` — `env_value` only.
   - `heartbeat/lib/send_telegram.py:280` — drop
     `os.environ.get("TELEGRAM_BOT_TOKEN")` from the token ladder
     (`--bot-token` arg and `.env` stay). The chat-id ladder
     (`TELEGRAM_CHAT_ID_OVERRIDE`/`ORIGIN_CHAT_ID` env) is routing, not auth
     — unchanged.
3. **Adapter subprocess env from allowlist** (`brains/base.py:366`). Replace
   `os.environ.copy()` with: whitelisted parent keys
   (`env_isolation.is_whitelisted` — HOME/PATH/LANG/TERM/LC_* etc.) +
   `JC_*`/`XDG_*` passthrough + `safe_instance_env_values(instance_dir)`
   (.env minus reserved keys) + the JC_/ORIGIN_CHAT_ID vars the method sets
   explicitly + `extra_env()`. A sibling token in the gateway's parent shell
   no longer reaches any brain subprocess.
4. **`sanitize()` filters dotenv.** `env_isolation.sanitize` gains
   `key_allowed: Callable[[str], bool] | None`; `bin/jc-gateway` passes
   `config.is_instance_env_key_allowed` so `.env` can no longer inject
   PATH/LD_PRELOAD/JC_INSTANCE_DIR over the whitelisted parent env.

## Feature 9 (doctor remainder) + Feature 10 (liveness slice)

**Files:** `lib/gateway/liveness.py`, new `lib/gateway/observability.py`,
`lib/gateway/runtime.py`, `bin/jc-doctor`,
`tests/gateway/test_liveness.py`, new `tests/gateway/test_observability.py`

### Ownership checks (Feature 9 — doctor side)
New findings in `liveness.py`, surfaced in a doctor "Ownership" section:
- **gateway process uid** — read `/proc/<pid>/status` Uid of the pidfile PID;
  FAIL when it differs from the instance dir owner (the root-contamination
  signature: gateway respawned as root).
- **state/ ownership** — spot-check `state/gateway`, `state/queue`,
  `state/watchdog` (+ first-level files): any entry owned by a different uid
  than the instance dir → FAIL (root-owned state breaks the jc-user
  supervisor with PermissionError).
- **root crontab JC-WATCHDOG** — when running as root (or `crontab -u root -l`
  is permitted): a `JC-WATCHDOG` block in root's crontab that names this
  instance → FAIL. Permission denied → INFO "cannot check root crontab (run
  doctor as root to verify)" — never a false green.

Deferred from Feature 9: bash-watchdog state move out of `/tmp`
(watchdog.sh:48) — changes live fleet watchdog behavior mid-flight; needs its
own rollout step with state migration. Follow-up stub in this spec.

### Real liveness (Feature 10 slice)
PID-up ≠ serving: today the heartbeat thread free-runs, so a deadlocked
dispatch loop passes every watchdog probe forever.

- Runtime tracks `_last_progress` (monotonic): bumped on every `run_forever`
  iteration AND on every successful lease renewal (`_LeaseHeartbeat` gains an
  `on_renew` callback) — a serial-mode gateway blocked inside a legitimate
  300s+ adapter call still renews its lease, so it still counts as alive.
- `_touch_heartbeat` **stops touching the heartbeat file** when
  `now - _last_progress > LIVENESS_STALL_SECONDS` (600 — generous vs. the
  ~100s lease-renew cadence and the per-poll loop tick, so no false stall on
  long tasks), logging `kind="liveness_stall"` (throttled). The watchdog's
  existing heartbeat-stale detection then sees a genuinely stale file and can
  remediate — no watchdog changes needed.
- Each heartbeat tick also writes `state/queue/liveness.json`
  (`{ts, progress_age_seconds}`, atomic) for observability.

### `jc doctor --json` (Feature 10 slice)
New `lib/gateway/observability.py`:
- `queue_metrics(instance_dir)` — depth by status, `oldest_queued_age_seconds`,
  failed count (sqlite read-only; absent DB → zeros).
- `snapshot(instance_dir)` — gateway PID finding, heartbeat-file age,
  `liveness.json` contents, queue metrics, `BrainFailureStore` entries,
  `channel_health.json` (Feature 4).
`bin/jc-doctor --json` prints `snapshot()` as JSON and exits (machine-readable
subset — the bash checks stay human-mode). The fleet-health skill can consume
this across the 24 agents (instance-repo change, out of scope here).

---

## Deferred items (explicit)

| Item | Why deferred | Follow-up |
|------|--------------|-----------|
| Feature 7 full schema unification | L-effort refactor across validator/loaders/3 writers + watchdog `_parse_yaml`; high regression surface | `docs/specs/jc-config-schema-unification.md` (stub, this PR) |
| Feature 4 SIGHUP live channel rebind | needs per-channel reload semantics design | noted in stub spec |
| Feature 5 live invoke probe | slow + quota-consuming; needs opt-in UX | noted in stub spec |
| Feature 9 watchdog `/tmp` state move | mutates live fleet watchdog state mid-flight; needs migration + rollout step | noted in stub spec |
| Part 1 P1/P2s outside features 1–10 (triage parser salvage, gemini yolo inversion, escaper fence ordering, claude/pi session capture race, …) | separate fix packages; audit doc remains the tracker | next audit phase |

## Tests

- `test_channel_supervision.py` — boot survives raising constructor; crashed
  channel restarts with backoff + rebuilt instance; quick clean first return →
  not-ready, no restart loop; stop_requested exits; health snapshot states.
- `test_brain_health.py` — probe ok/fail per missing adapter/binary/auth;
  fallback-role failure detection; store TTL expiry; clear-on-recovery.
- `test_telegram_outbound_hardening.py` — chunking (boundaries, fences,
  escaped-length fit, reply-to on first chunk only, last message_id), 429
  retry honored then raises, voice caption truncate-then-escape, send_photo
  not-ok logged + None.
- telegram poll: enqueue failure rewinds offset + retries next poll; 3rd
  failure advances with drop log.
- config: YAML syntax error raises ConfigError (no silent fallback); nested
  triage threshold 7 rejected; `max_retries: 0` / `log_backups: 0` preserved;
  `triage_cache_ttl_seconds` accepted top-level; supervisor narrator brain
  typo rejected; `set_supervisor_enabled` validates + writes atomically.
- env: secret key absent from .env resolves empty even when present in
  os.environ; adapter env contains no unlisted parent keys but keeps
  PATH/HOME/JC_*/.env values; sanitize drops reserved dotenv keys.
- observability: queue metrics on a seeded queue.db; snapshot shape;
  heartbeat suppression after simulated stall (unit-level on
  `_touch_heartbeat` gate).
