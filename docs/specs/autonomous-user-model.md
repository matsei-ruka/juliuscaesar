# Spec: Autonomous User Model Updates

**Status:** Draft — pending Luca review
**Scope:** JuliusCaesar `lib/user_model/` — detect patterns across sessions, propose or auto-apply updates to `<instance>/memory/L1/USER.md` (and adjacent L2 entries) without requiring an explicit user correction.
**Branch:** `feat/autonomous-user-model`

---

## Goal

Today the user model in `<instance>/memory/L1/USER.md` only changes when the user explicitly corrects the agent. Drift accumulates: the user mentions a new partner, shifts focus, repeats a preference — and the L1 file stays frozen until someone hand-edits it. We want JC to read its own conversation history, detect those signals, and either auto-apply high-confidence updates or surface a diff for human approval.

This is opt-in per instance, defaults to off, and never silently overwrites without leaving a backup. Privacy filters run before any session content reaches detection. The feature runs out-of-band on a cron schedule so it has zero impact on live response latency.

---

## Deviations from worker brief (call out for review)

The original worker prompt made three assumptions that did not hold against the current codebase. The spec proposes alternatives — flag if you disagree:

1. **Session history source.** Prompt said `lib/memory/sessions/*.sqlite` with FTS5. Reality: `lib/memory/db.py` is the L1/L2 wiki index (entries + entries_fts), it does not store conversation messages. The actual conversation log is the gateway event queue at `<instance>/state/gateway/queue.db`, table `events`, with `content` (user message), `response` (brain reply), `received_at`, `conversation_id`, `user_id`. Brain-native transcripts (`~/.claude/projects/<slug>/*.jsonl`) carry richer detail (tool calls, reasoning) but are Claude-specific. **Spec uses the events table as the primary source** and treats Claude jsonl as an optional enrichment in a follow-up sprint.

2. **Scheduling location.** Prompt said `<instance>/heartbeat/tasks.yaml`. Reality: heartbeat (`lib/heartbeat/runner.py`) is LLM-adapter-only — every task is `prompt → adapters/<tool>.sh → Telegram delivery`. This feature is a hybrid pipeline (Python detector + structured LLM proposer + diff-style notifier), which doesn't fit the heartbeat shape. **Spec adds a system cron line installed by `jc user-model install`**, sibling to the existing `jc-watchdog` cron line. Heartbeat stays lean.

3. **Config file.** Prompt said extend `gateway.yaml`. Reality: `gateway.yaml` is scoped to gateway/channels/triage/brains. Mixing memory-mutation policy in there would couple unrelated subsystems. **Spec puts config in a new file `<instance>/ops/user_model.yaml`** loaded by `lib/user_model/conf.py`. Pattern matches how `lib/company/conf.py` owns its own config surface.

If any of these are wrong, ping back and the spec gets revised.

---

## Trigger / cadence

- Default: daily at `03:00` instance-tz (cron `0 3 * * *`).
- Skippable: if `events.received_at` max is older than the last successful run's checkpoint, exit fast (no-op + log line).
- Single-flight: per-instance flock at `<instance>/state/user_model/run.lock`. Overlapping runs no-op.
- Manual trigger: `jc user-model run-now`.
- First run on an instance: bootstrap path — read all events but cap at the most recent `look_back_days * 5` to avoid unbounded scan; log explicitly that it's a bootstrap.

Cron line installed by `jc user-model install`:

```
0 3 * * * /home/$USER/.local/bin/jc-user-model run --instance-dir /path/to/instance  # jc-user-model for /path/to/instance
```

`jc user-model uninstall` removes it. Both commands edit the user crontab via `crontab -l | crontab -` round-trip, mirroring how the framework already handles watchdog entries.

---

## Input data sources

| Source | Path | Used for |
|--------|------|----------|
| Gateway events queue | `<instance>/state/gateway/queue.db` table `events` | User messages + responses, by `(user_id, conversation_id, received_at)`. Primary corpus for detection. |
| Existing user model | `<instance>/memory/L1/USER.md` | Diffs against current state — every proposal is "from X to Y", never blind insert. |
| Other L1 files | `<instance>/memory/L1/{IDENTITY,RULES,HOT,CHATS}.md` | Read-only context for LLM proposer (don't propose changes that contradict RULES.md). |
| L2 entries | `<instance>/memory/L2/people/`, `<instance>/memory/L2/learnings/`, `<instance>/memory/L2/business/` | Looked up via `jc memory search` to dedupe (don't propose adding "Martina" to USER.md if `people/martina.md` already exists; instead propose a wikilink). |
| L2 frontmatter dates | `created`, `updated`, `last_verified` from each entry | Treat older entries as stable; recent corrections in `learnings/` carry higher weight. |

**Schema reference (verified 2026-04-28):**

```sql
-- lib/gateway/queue.py:96-114
events(id, source, source_message_id, user_id, conversation_id,
       content, meta, status, received_at, available_at,
       locked_by, locked_until, started_at, finished_at,
       retry_count, response, error)
```

Useful indexes already exist: `idx_events_conversation(source, user_id, conversation_id, received_at DESC)`.

The detector reads this DB read-only — never writes. A separate connection with `?mode=ro` is used.

---

## Detection algorithms

Five passes run sequentially. Each pass yields zero or more candidate signals; a signal is one observation, not yet a proposal. Signals are aggregated and deduped before being handed to the proposer.

### 1. Recurring-topic detector

Tokenize last `look_back_days` of `events.content`, drop stopwords + already-mentioned tokens from current `USER.md`, count term frequency, surface terms with `count >= min_evidence_count` (default 3) that appear across at least 2 distinct conversations. Output: `{kind: "recurring_topic", term, count, sample_event_ids}`.

### 2. Communication-preference detector

Heuristic over recent `events`:
- Average user message length → preferred reply length signal.
- Voice messages vs text ratio (from `meta.is_voice`) → voice-mode preference.
- Frequency of meta-feedback tokens ("don't", "stop", "shorter", "longer", "no fluff") → tone signals.

Output: `{kind: "comm_pref", dimension, current_value, observed_value}`. Only fires if delta > threshold.

### 3. Project-priority drift

Compare entity frequency (people, businesses, project slugs found via wikilink-style mentions or simple noun chunks) in this period vs the prior period of equal length. Surface entities with `delta_pct > 50%` and `current_count >= min_evidence_count`. Output: `{kind: "priority_shift", entity, prev_count, curr_count, delta}`.

### 4. New-named-entity detector

Run a lightweight NER pass (spacy if installed, else heuristic regex for capitalized n-grams + "Mr/Ms/Mrs/Dr/CEO/CTO/at <Org>") over recent `events.content`. For each unique entity, query `jc memory search "<entity>"` — if no L2 hit and the entity appears `>= min_evidence_count` times across `>= 2` conversations, signal it. Output: `{kind: "new_entity", entity, mentions, sample_event_ids}`.

### 5. Rule-drift detector

For each line in `<instance>/memory/L1/RULES.md` that looks like a rule (bullet starting with `**X**` or imperative), search recent events for contradiction. Implementation: extract rule keywords (e.g. "MarkdownV2", "07:00 and 19:00"), look for events where the user appears to have asked for the opposite. This is the riskiest pass — proposals from this detector are **always** mode `propose`, never auto-applied, regardless of confidence threshold.

Output: `{kind: "rule_drift", rule_excerpt, contradicting_event_ids, severity}`.

### Privacy filter (runs before all detectors)

Sessions are dropped from analysis if any of these match `events.content`:

- Hard-coded regex blocklist for explicit sexual content, credentials (`/sk-[A-Za-z0-9]{30,}/`, AWS keys, etc.), and other RULES.md privacy markers.
- Optional second-pass LLM filter (`claude-haiku-4-5`, ~$0.001 per session) returning `{safe: true|false}`. Only runs if `privacy_filter.llm_pass: true` in config (default off; hard-block list is sufficient for v1).
- A session that hits any filter is dropped wholesale — partial redaction is too error-prone.

Why aggressive: per `RULES.md` privacy rule, no erotic/sexual content gets persisted. A signal derived from a filtered session would be derived from that content. Drop the whole session.

---

## Output format

Detector signals are folded into one or more proposals by the proposer module. A proposal looks like this on disk (one JSON object per line in JSONL):

```json
{
  "id": "20260428-a1b2c3",
  "created_at": "2026-04-28T03:00:14Z",
  "type": "modify",
  "target_file": "memory/L1/USER.md",
  "target_section": "## Family",
  "current_content": "- Daughter [[people/martina|Martina]] — 6yo, Year 2, autistic, super smart.",
  "proposed_content": "- Daughter [[people/martina|Martina]] — 6yo, Year 2, autistic, super smart. School: Dove Green.",
  "reasoning": "Luca mentioned Dove Green in 7 conversations over 14 days. Existing entry omits school name despite L2 entry people/martina mentioning it.",
  "confidence": 0.92,
  "supporting_evidence": ["event:14523", "event:14687", "event:15102"],
  "content_hash": "sha256:7f3a..."
}
```

Field semantics:

- `type`: `add` | `modify` | `remove`. `remove` is rare and capped at `confidence < 0.95` minimum to ever auto-apply.
- `target_file`: relative to instance dir. The applier refuses any path outside `<instance>/memory/`.
- `target_section`: heading text or `null` for top-level inserts. Applier matches by heading text, never line number (line numbers drift).
- `current_content`: exact byte-equal string from the current file. Applier verifies match before replacing — if drift, proposal is parked in `stale/`.
- `confidence`: 0.0–1.0. Calibration table in `lib/user_model/proposer.py` docstring.
- `content_hash`: `sha256(target_file + target_section + proposed_content)` — used for dedup across runs.

Proposals live in `<instance>/memory/staging/user-model-proposals.jsonl`. Applied/rejected proposals migrate to `applied.jsonl` / `rejected.jsonl` with a terminal-state record.

---

## Apply modes

Config flag `apply_mode`:

| Mode | Behavior |
|------|----------|
| `disabled` | Feature off. `run-now` returns 0 immediately. **Default for any unconfigured instance.** |
| `propose` | Detect → propose → write to `staging/user-model-proposals.jsonl` → Telegram-notify owner with summary + colored diff. Owner runs `jc user-model apply <id>` or `reject <id>`. **Default once enabled.** |
| `auto_high_confidence` | Auto-apply proposals with `confidence >= confidence_threshold` (default 0.85). Notify owner of what changed. Lower-confidence proposals fall back to `propose` behavior. Rule-drift proposals (detector pass 5) are never auto-applied — always require explicit approval. |
| `auto_all` | Auto-apply everything. Audit log only. **Not recommended; gated behind a `--i-know-what-im-doing` flag at install time.** |

Audit log: `<instance>/memory/staging/audit.jsonl`, append-only, never rewritten.

---

## Config schema

New file `<instance>/ops/user_model.yaml`:

```yaml
enabled: false                  # bool. Default off. Must be true for any cron run to do work.
apply_mode: propose             # disabled | propose | auto_high_confidence | auto_all
cadence_cron: "0 3 * * *"       # crontab expression for jc user-model install
look_back_days: 7
min_evidence_count: 3
confidence_threshold: 0.85      # only consulted when apply_mode == auto_high_confidence
proposal_cooldown_days: 30      # don't re-propose the same content_hash for N days
notify_chat_id: null            # if null, fall back to .env TELEGRAM_CHAT_ID
proposer_model: claude-sonnet-4-6
privacy_filter:
  llm_pass: false               # second-pass LLM filter on top of regex blocklist
  llm_model: claude-haiku-4-5
detectors:
  recurring_topic: true
  comm_pref: true
  priority_shift: true
  new_entity: true
  rule_drift: true              # proposals from this detector are always propose-mode regardless
```

Loaded by `lib/user_model/conf.py` into a frozen dataclass (`UserModelConfig`). Same shape pattern as `lib/gateway/config.py:GatewayConfig`. Missing file → returns `UserModelConfig(enabled=False)` and logs a one-line info message.

---

## Module layout

```
juliuscaesar/lib/user_model/
├── __init__.py
├── conf.py                 # UserModelConfig dataclass + load_config(instance_dir)
├── corpus.py               # Read events from queue.db; apply privacy filter
├── detector.py             # Five detectors → list[Signal]
├── proposer.py             # Aggregate signals, call LLM, emit Proposal objects
├── applier.py              # Atomic write to memory files + .history backup
├── notifier.py             # Telegram diff-summary message (uses send_telegram lib)
├── store.py                # JSONL read/write for staging/applied/rejected
├── runner.py               # Top-level run() — wires it all together; called by cron + CLI
└── cli.py                  # argparse subcommands

juliuscaesar/bin/jc-user-model       # shim that imports lib.user_model.cli:main

juliuscaesar/tests/user_model/
├── conftest.py             # fixture instance dir with synthetic events
├── test_corpus.py
├── test_detector.py
├── test_proposer.py
├── test_applier.py
├── test_privacy_filter.py  # red-team fixtures: erotic, credentials, PII
├── test_dedup.py
└── test_e2e.py             # full pipeline against fixture instance
```

No changes to `lib/gateway/`, `lib/heartbeat/`, or `lib/memory/`. Read-only consumers of those subsystems' on-disk artifacts.

---

## CLI surface

```
jc user-model install [--instance-dir DIR] [--cadence "0 3 * * *"]
                                               # writes config skeleton + cron line
jc user-model uninstall [--instance-dir DIR]   # removes cron line + leaves config in place
jc user-model run-now                          # synchronous run, exit code 0/1
jc user-model status                           # last-run timestamp, pending count, mode
jc user-model review [--id ID | --limit N]     # print pending proposals (paginated)
jc user-model apply <id>                       # promote staging → applied, write to memory
jc user-model reject <id> [--reason "..."]     # promote staging → rejected, log reason
jc user-model audit [--since DATE]             # tail audit.jsonl
```

`run-now` is what the cron entry calls under the hood (`jc-user-model run --instance-dir ...`).

---

## Edge cases

| Case | Behavior |
|------|----------|
| User contradicts `RULES.md` rule | Detector pass 5 fires. Proposal is **always** `apply_mode: propose` regardless of config or confidence. Telegram message labels it `⚠️ rule-drift`. |
| Erotic / sexual content in session | Privacy filter drops the whole session. Logged as `dropped: privacy` (count only — no content). Verified by red-team fixture in `test_privacy_filter.py`. |
| Duplicate proposals across runs | `content_hash` dedup. Same hash within `proposal_cooldown_days` → silently skipped. Cleared if owner explicitly rejected (no point re-proposing) or if cooldown expires. |
| First run on instance with no events | `corpus.iter_events()` returns empty → no signals → no proposals → exit 0 + log `no events in window`. |
| Field already covered by L2 | If `new_entity` detector finds "Dove Green" but `jc memory search "Dove Green"` returns a hit in `L2/`, propose a wikilink update to `L1/USER.md` referencing the existing L2 entry, not a duplicate L2 creation. |
| `current_content` mismatch (file edited between proposal + apply) | Applier refuses, marks proposal `state: stale`, owner re-runs `run-now` to regenerate. |
| Concurrent `apply` calls | File-level flock on `memory/L1/USER.md` (per applier op). |
| Ambiguous proposals (low confidence, conflicting signals) | Proposer collapses into a single proposal with `confidence < threshold` and a `reasoning` field that explains the ambiguity. Falls into `propose` even in auto-mode. |
| Owner not present (no chat_id, all notify channels off) | Proposals still write to staging. `jc user-model status` is the fallback surface. |

---

## Atomicity & backups

Memory file writes:

```python
# lib/user_model/applier.py:apply_proposal
backup = instance_dir / "memory" / ".history" / f"USER.md.{ts}"
backup.write_bytes(target.read_bytes())
tmp = target.with_suffix(target.suffix + ".tmp")
tmp.write_text(new_content, encoding="utf-8")
os.replace(tmp, target)
```

`.history/` is added to `.gitignore` for instance repos (it can grow unbounded; cleanup is a future concern, not blocking for this spec).

Both `staging/*.jsonl` and `audit.jsonl` are append-only. Rotation past 10 MiB is a future concern — log a warning at 50 MiB, fail at 200 MiB.

---

## Migration plan

- **Default state:** disabled. Existing instances see no behavior change.
- **Opt-in steps documented in QUICKSTART:**
  1. `jc user-model install --instance-dir <path>` — writes `ops/user_model.yaml` skeleton + cron line.
  2. Edit `ops/user_model.yaml` to set `enabled: true` and choose `apply_mode`.
  3. (Optional) `jc user-model run-now --dry-run` to preview proposals.
- **No breaking changes** to existing config files. `gateway.yaml`, `tasks.yaml`, `.env` untouched.
- `CLAUDE.md` `@import` chain not affected — we read L1, never block its loading.

---

## Test plan

- **Unit tests** per detector, with fixture event lists asserting expected signals.
- **Privacy filter red-team:** fixtures containing explicit content, leaked credentials, PII. Assert filter drops the session and no signal escapes.
- **Applier atomicity test:** kill the process mid-write (via `os.kill(os.getpid(), SIGKILL)` in a forked child), assert original file intact + backup intact + tmp file orphaned-but-harmless.
- **Dedup test:** run pipeline twice with identical fixture events, assert second run produces zero new staging entries.
- **End-to-end:** synthetic instance dir + queue.db + USER.md, run full pipeline, assert proposal written, apply, assert USER.md changed + .history backup present.
- **Confidence calibration:** ten labeled scenarios with expected `confidence` ranges, asserted within ±0.1.

Target: `pytest tests/user_model/` green on the feature branch before PR.

---

## Rollout

| Phase | When | Action |
|-------|------|--------|
| Canary | Day 0 (PR merge) | Rachel instance only, `apply_mode: propose`. Luca approves every proposal manually for 2 weeks. Telemetry: count of proposals, accept rate, rejection reasons. |
| Beta | Day 14, if accept rate > 60% and zero privacy-filter escapes | Rachel can move to `auto_high_confidence` if Luca opts in. Sergio's instance offered the `propose` mode (still default off until explicitly enabled). |
| GA | Day 30+ | Documented in QUICKSTART, mentioned in README. Default still `disabled`. |

---

## Open questions

1. **LLM proposer cost.** Each run sends `look_back_days * avg_events_per_day` of content to the proposer model. For Rachel that's roughly 7 days × ~50 messages = 350 events ≈ ~30k tokens. At Sonnet rates that's ~$0.10 per run, $3/month. Acceptable, but if multiple instances enable this it's worth caching. **Resolution:** v1 has no cache; revisit if cost becomes annoying.

2. **NER quality.** Pass 4 (`new_entity`) is the highest-noise detector. Heuristic regex will surface false positives ("New York" mentioned once in a news context). v1 mitigates with `min_evidence_count >= 3` + `>= 2` distinct conversations. Spacy NER would be better; adds a dependency. **Resolution:** ship heuristic v1, add spacy in a follow-up if false-positive rate is high.

3. **Multi-user instances.** `events.user_id` is per-user. Current spec only writes a single `USER.md` — if an instance has multiple authorized users, the "user model" concept is ambiguous. **Resolution:** v1 reads only the instance owner's `user_id` (configured in `user_model.yaml` as `owner_user_id`, defaulting to the most-frequent `user_id` in events). Multi-user is a future expansion.

4. **Rule-drift detector — should it auto-disable when severity is high?** Detector pass 5 might surface that the user has been asking for behavior that contradicts a rule they themselves wrote. v1 just notifies. Future: it could also propose a *RULES.md* edit, not just a USER.md edit. **Resolution:** out of scope for v1. Filed as a follow-up.

5. **Should `auto_all` exist at all?** It's documented but the install flag locks it behind `--i-know-what-im-doing`. The conservative answer is to ship without it; users who want yolo can edit the YAML directly. **Resolution:** ship the mode, gate the install flag, document the risk.

---

## KB updates needed (post-implementation)

After Phase 2, propose `/kb update` for these entries (their `code_anchors` will not change but new related-entries should be cross-linked):

- `docs/kb/subsystem/memory-system.md` — note the new `memory/staging/` and `memory/.history/` directories under instance memory layout.
- `docs/kb/contract/instance-layout-and-resolution.md` — add `ops/user_model.yaml`, `state/user_model/`, `memory/staging/`, `memory/.history/`.
- `docs/kb/contract/config-and-secret-boundaries.md` — register `ops/user_model.yaml` as a new instance config file (no secrets — pure policy).
- New entry: `docs/kb/subsystem/autonomous-user-model.md` — own subsystem entry with code anchors into `lib/user_model/`.

---

## Sprints (Phase 2 outline)

### Sprint 1 — Read path + detection
- `corpus.py` reads events with privacy filter applied. Tests cover red-team fixtures.
- `detector.py` implements all 5 passes. Each unit-tested with synthetic event fixtures.
- `cli.py status / review` work end-to-end (no proposer, no applier yet).

### Sprint 2 — Proposer + storage
- `proposer.py` calls LLM with structured output, emits `Proposal` objects.
- `store.py` JSONL read/write + `content_hash` dedup + cooldown.
- `cli.py run-now` produces staging entries; no apply yet.

### Sprint 3 — Applier + notifier
- `applier.py` atomic write + backup + flock.
- `notifier.py` Telegram diff-summary using `lib/heartbeat/lib/send_telegram.sh`.
- `cli.py apply / reject / audit`.
- `auto_high_confidence` and `auto_all` modes wired.

### Sprint 4 — Install + docs
- `cli.py install / uninstall` with crontab manipulation.
- QUICKSTART section.
- Test e2e on Rachel's instance with `apply_mode: propose`. Verify Telegram notification arrives.
- Open draft PR.

---

## Definition of done (per worker brief)

- [ ] Spec at `juliuscaesar/docs/specs/autonomous-user-model.md` on `feat/autonomous-user-model` — Phase 1 deliverable.
- [ ] All five detectors implemented + unit-tested.
- [ ] Privacy filter passes red-team fixtures with zero leaks.
- [ ] Applier atomic; `.history` backup verified.
- [ ] CLI subcommands all work.
- [ ] `pytest tests/user_model/` green.
- [ ] Rachel instance running in `propose` mode end-to-end with at least one proposal generated and reviewed.
- [ ] Draft PR open, title `feat: autonomous user model updates`, no merge.
- [ ] KB entries updated.
