# Spec: Dreaming & Self-Improvement

**Status:** Draft — pending review
**Date:** 2026-05-11
**Branch:** `spec/dreaming-and-self-improve`
**Scope:** Unified self-learning / self-improvement subsystem for JC instances. Subsumes the in-flight `commitments-and-reengage` draft. Extends the existing `lib/self_model/` engine. Introduces a nightly offline reflection pipeline analogous to Anthropic's "Dreams" managed-agents feature.

**Companion sub-specs (this PR):**
- `docs/specs/commitments-and-reengage.md` — the action engine + silence detector that the dream pipeline produces work for.

---

## 0. Why this exists

JC agents today have **three independent half-feedback-loops**, none of which close cleanly:

1. **`lib/self_model/`** — already detects pattern signals (submission drift, rule inadequacy, error recurrence) from transcripts and proposes diffs to `RULES.md` / `JOURNAL.md`. Runs on-demand. **Not scheduled, not consolidating memory, not emitting playbooks.**
2. **`commitments-and-reengage` (draft)** — deferred-action engine + silence detector. **Producers are ad-hoc** (agent-during-conversation, manual). Mario Leone instance ships a bespoke `ops/commitments-tick.py` that needs to graduate to framework code.
3. **Anthropic Claude Dreams** (research-preview, May 2026) — between-session reflection that reads transcripts + memory, **consolidates dedup'd memory, emits playbooks**, leaves weights alone. Reported ~6× task-completion lift at Harvey, 50% reduction in document-review time at Wisedocs.

These three are the same pipeline drawn from three vantage points. JC will collapse them into one nightly offline cycle, hereafter `jc dream`, with three artifacts: **memory updates**, **playbook entries**, and **commitments** (including re-engagement touches).

The `lib/self_model/` runtime engine stays — it becomes the *consolidator* and *codifier* stage of the dream pipeline rather than a parallel system.

---

## 1. Naming & framing

Pick the words once, use them everywhere:

| Term | Meaning |
|---|---|
| **Dream** | One execution of the offline reflection cycle on an instance. Produces a *dream report* (the audit trail) plus *artifacts* (memory diffs, playbooks, commitments). |
| **Dream report** | A markdown record of one cycle, stored at `state/dreams/<UTC-iso>.md`. Lists inputs scanned, signals detected, artifacts emitted, and approval state. |
| **Playbook** | A structured procedural note distilled from one or more transcripts: "in situation X, the pattern that worked was Y." Living document, versioned, owned by the agent (curated by operator). Lives at `memory/L2/playbooks/<slug>.md`. |
| **Learning** | A discovered fact about the user, the world, or the agent itself, written into `memory/L2/learnings/<slug>.md`. (Existing convention; dream is the major producer.) |
| **Commitment** | A future-tense action queued by any producer. Single source of truth: `state/commitments/<slug>.yaml`. (Specified in `commitments-and-reengage.md`.) |
| **Sweep** | A periodic audit producing a markdown *report*, not artifacts. Out of scope here; mentioned for boundary clarity. |

We do **not** call this layer "memory consolidation" or "reflection" externally — those terms are too generic. The user-facing term is **Dream**. The internal module is `lib/dream/`. Subsystems stay named for their function: `lib/commitments/`, `lib/reengage/`, `lib/self_model/`.

---

## 2. Architecture

```
                  ┌─────────────────────────────────────────────────┐
                  │  Nightly trigger (heartbeat builtin: dream_tick)│
                  │  Default: 03:30 instance-local, opt-in          │
                  └─────────────────────────┬───────────────────────┘
                                            ▼
       ┌────────────────────────────────────────────────────────────────┐
       │ Phase 1 — Reflect (lib/dream/reflect.py)                       │
       │  • Compute window: (last_dream_ts, now)                        │
       │  • Read state/transcripts/*.jsonl deltas in window             │
       │  • Read memory/INDEX.md state hash + L1 modification times     │
       │  • Read state/heartbeat/sent.log deltas (what we said offline) │
       │  • Read state/commitments/done/ entries closed in window       │
       │  • Output: Reflection bundle (in-memory dataclass)             │
       └─────────────────────────────┬──────────────────────────────────┘
                                     ▼
       ┌────────────────────────────────────────────────────────────────┐
       │ Phase 2 — Consolidate (lib/dream/consolidate.py)               │
       │  • Wraps lib/self_model/detector.py — same Signal type         │
       │  • Adds: duplicate-entry detection in L2                       │
       │           contradiction detection (frontmatter `state: …`)     │
       │           backlink integrity check                             │
       │           stale-timestamp detection                            │
       │  • Output: ConsolidationFindings                               │
       └─────────────────────────────┬──────────────────────────────────┘
                                     ▼
       ┌────────────────────────────────────────────────────────────────┐
       │ Phase 3 — Codify (lib/dream/codify.py)                         │
       │  • Wraps lib/self_model/proposer.py — same LLM call surface    │
       │  • Adds three new artifact emitters:                           │
       │      - playbook_emitter  → memory/L2/playbooks/*.md            │
       │      - learning_emitter  → memory/L2/learnings/*.md            │
       │      - commitment_emitter → state/commitments/*.yaml           │
       │  • Wraps lib/reengage/queuer.py as one commitment_emitter      │
       │  • Output: Proposed artifact set (diffs, not files yet)        │
       └─────────────────────────────┬──────────────────────────────────┘
                                     ▼
       ┌────────────────────────────────────────────────────────────────┐
       │ Phase 4 — Apply (lib/dream/apply.py)                           │
       │  • Wraps lib/self_model/applier.py for SENSITIVE diffs         │
       │  • Auto-apply low-risk classes (see §6 risk matrix)            │
       │  • Stage SENSITIVE diffs for DKIM-gated operator approval      │
       │  • Write dream report: state/dreams/<UTC>.md                   │
       │  • Telegram-notify operator: "1 dream done. N pending diffs."  │
       └────────────────────────────────────────────────────────────────┘
```

Every phase is a pure function (or as close as IO permits): inputs flow in, outputs flow out. No phase mutates state of a previous phase. Apply is the only phase that writes user-visible files; everything before it is read-only against the instance.

---

## 3. Module layout

```
juliuscaesar/
├── bin/
│   ├── jc-dream                       # NEW — top-level CLI
│   └── jc-commitments                 # from commitments-and-reengage
├── lib/
│   ├── dream/                         # NEW — orchestrator + phase modules
│   │   ├── __init__.py
│   │   ├── conf.py                    # ops/dream.yaml loader
│   │   ├── cli.py                     # subcommand dispatch
│   │   ├── reflect.py                 # phase 1
│   │   ├── consolidate.py             # phase 2 (wraps self_model.detector)
│   │   ├── codify.py                  # phase 3 (wraps self_model.proposer + new emitters)
│   │   ├── apply.py                   # phase 4 (wraps self_model.applier + auto-apply rules)
│   │   ├── report.py                  # dream-report markdown writer
│   │   ├── emitters/
│   │   │   ├── playbook.py
│   │   │   ├── learning.py
│   │   │   └── commitment.py          # bridge to commitments engine
│   │   └── schema.py                  # Reflection, ConsolidationFindings, ProposedArtifact
│   ├── self_model/                    # EXISTING — unchanged surface; consumed by dream
│   ├── commitments/                   # from commitments-and-reengage
│   └── reengage/                      # from commitments-and-reengage
└── docs/specs/
    ├── dreaming-and-self-improve.md   # this file (umbrella)
    └── commitments-and-reengage.md    # sub-spec
```

**`lib/self_model/` is not renamed.** It is the consolidator/codifier *engine*; `lib/dream/` is the *scheduler + orchestrator + new emitters*. Renaming would generate a churn diff with no behavior change.

---

## 4. Subcommands

```
jc-dream tick                      # one cycle (used by heartbeat builtin)
jc-dream run --since <iso> --until <iso>   # manual replay over a window
jc-dream dry-run                   # phases 1–3 only; no apply, no report write
jc-dream list                      # ls state/dreams/, newest first
jc-dream show <utc>                # cat state/dreams/<utc>.md
jc-dream pending                   # list staged SENSITIVE diffs awaiting approval
jc-dream approve <diff-id>         # operator approval gate (DKIM equivalent)
jc-dream reject <diff-id>          # remove staged diff with operator note
```

Composition: `jc-dream tick` is the only one cron calls; everything else is operator-facing.

---

## 5. Data model

### 5.1 Reflection bundle (in-memory)

```python
@dataclass(frozen=True)
class Reflection:
    window_start: datetime         # UTC
    window_end:   datetime         # UTC
    transcript_deltas: list[TranscriptDelta]   # per chat_id
    memory_state_hash: str         # snapshot of L1 + L2 frontmatter only
    sent_deltas: list[SentRecord]  # outbound from heartbeat
    closed_commitments: list[CommitmentRecord]  # done/ entries in window
```

`TranscriptDelta` references `state/transcripts/<chat_id>.jsonl` by byte offset range so we don't load the whole file. The dream pipeline never *modifies* transcripts; gateway is the only writer.

### 5.2 ConsolidationFindings

```python
@dataclass(frozen=True)
class ConsolidationFindings:
    signals: list[Signal]                  # from self_model.detector — unchanged type
    duplicates: list[DuplicateGroup]       # NEW — L2 entries with overlapping slugs/aliases
    contradictions: list[Contradiction]    # NEW — frontmatter state conflicts
    broken_backlinks: list[BrokenLink]     # NEW — [[wikilink]] targets that don't exist
    stale_timestamps: list[StaleEntry]     # NEW — last_verified older than threshold
```

### 5.3 ProposedArtifact (new — sealed sum type)

```python
ProposedArtifact = (
    MemoryDiff           # patch to memory/L2/<path>.md
  | PlaybookEntry        # new file under memory/L2/playbooks/
  | LearningEntry        # new file under memory/L2/learnings/
  | CommitmentYAML       # write state/commitments/<slug>.yaml via commitments-engine
  | RulesProposal        # existing self_model proposal — DKIM-gated
  | IdentityProposal     # existing self_model proposal — DKIM-gated, IDENTITY.md only with explicit operator unlock
)
```

Each ProposedArtifact carries: `risk_class` (LOW / MEDIUM / SENSITIVE), `source_signals` (refs into the Reflection), `proposed_action` (apply / stage / reject-self), and a stable `diff_id` (sha256 of canonical content).

### 5.4 Playbook schema

`memory/L2/playbooks/<slug>.md`:

```markdown
---
slug: playbooks/<slug>
title: <human title>
layer: L2
type: playbook
state: <draft | active | retired>
created: 2026-05-12
updated: 2026-05-12
last_verified: 2026-05-12
provenance: dream/<utc>
tags: [playbook, ...]
trigger: <condition phrase used by retrieval — see below>
links: []
---

# <title>

## When to use
<one paragraph; the trigger condition>

## Procedure
1. <step>
2. <step>
3. <step>

## Anti-patterns
- <what not to do>

## Source dreams
- dream/<utc> — <one line on what observation produced this>
```

**Retrieval:** playbooks are pulled via `jc memory search` like any L2 entry. The `trigger` frontmatter field is what makes a playbook a playbook — it's a phrase or condition the agent uses as a self-query at the start of a conversation: "does any active playbook trigger match this situation?"

### 5.5 Dream report

`state/dreams/<UTC-iso>.md` — markdown audit trail. Schema:

```markdown
---
dream_id: <utc>
window: [<start>, <end>]
duration_ms: <int>
brain: <model used for codify phase>
status: completed | partial | failed
artifacts:
  memory_diffs:   <int auto> + <int staged>
  playbooks:      <int created>
  learnings:      <int created>
  commitments:    <int queued>
  rules_drafts:   <int staged>
---

# Dream <utc>

## Reflection summary
<one paragraph: what was observed>

## Consolidation findings
- <signal | duplicate | contradiction | broken backlink | stale timestamp>

## Artifacts emitted
- AUTO_APPLIED: <diff-id> <one-liner>
- STAGED:       <diff-id> <one-liner>  [pending operator approval]
- REJECTED_SELF: <diff-id> <reason>

## Followups
<commitments queued, with due_at>
```

---

## 6. Risk classification & approval

The auto-apply / stage decision is the most-consequential design choice in this spec.

### 6.1 Risk classes

| Class | Definition | Default policy |
|---|---|---|
| **LOW** | Reversible, non-content. Examples: fix broken `[[wikilink]]`, bump `last_verified`, merge two identical L2 entries (sha256 match). | **Auto-apply.** Logged in report. |
| **MEDIUM** | New L2 content, no overwrite of existing content. Examples: new playbook, new learning, new memory entry under a never-used slug. | **Auto-apply** with a 24-hour "soft retain" — operator can `jc-dream reject <diff-id>` until next dream to roll back. |
| **SENSITIVE** | Edits to existing L1 files (RULES.md, USER.md, IDENTITY.md, STYLE.md), edits to existing L2 with content change > 20% diff, edits that touch any IMMUTABILE / `<!-- FROZEN -->` section. | **Stage only.** Operator approval via `jc-dream approve <diff-id>` or DKIM-signed email (same gate the existing `self_model.applier` uses). |

The classifier lives at `lib/dream/risk.py`. It is **the only correct surface** to add a new risk rule; `lib/self_model/frozen_sections.py` continues to enforce the IMMUTABILE invariant at apply-time as a defense-in-depth check.

### 6.2 IMMUTABILE invariant

A diff that targets any IMMUTABILE / `<!-- FROZEN -->` section is **rejected before being staged**, never auto-applied, and the dream report logs it as `REJECTED_SELF`. This is unchanged from current `self_model.applier` behavior; the dream wrapper inherits the same guard.

### 6.3 Audit trail

Every applied or staged diff is written to `state/dreams/<utc>.md` with its `diff_id`. The `state/dreams/` directory is append-only (no UPDATE / DELETE in `apply.py`). The operator can `git diff` the instance repo to see exactly what each dream changed; this is the audit log.

---

## 7. Scheduling & triggers

### 7.1 Default schedule

Nightly, instance-local **03:30**, via `heartbeat/tasks.yaml`:

```yaml
dream_tick:
  builtin: dream_tick
  enabled: false              # operator opts in
  default_schedule: "30 3 * * *"
```

03:30 is chosen because:
- After 23:00–07:00 quiet hours (`USER.md` rule).
- Before 07:00 proactive-ideas window — gives the morning briefing access to fresh playbooks.
- Off-peak for Anthropic API (lower contention if Claude is the codifier brain).

### 7.2 Manual triggers

- `jc-dream tick` — one immediate cycle. Same code path as cron.
- `jc-dream run --since X --until Y` — replay a closed window. Idempotent: writes to `state/dreams/replay-<utc>.md`, never overwrites a prior live dream.
- Reactive trigger via jc-event: if the gateway emits `episode_close` (a new event type defined in this spec, fired when a long thread ends), a small dream can run on-demand over that single thread. Out of scope for v1; schema-ready.

### 7.3 Cost & duration budget

- **Soft target:** one dream completes in < 90 s for an instance with ≤ 1000 transcript lines/day and ≤ 5000 L2 entries.
- **Hard timeout:** 600 s. Beyond that, `phase` is set to `partial`, report still written, missing phases flagged. Tomorrow's dream picks up by widening the `window_start`.
- **Token cost:** worst-case ≈ one `claude --print` call per consolidation chunk (target: 4 chunks/dream). Daily ceiling ≈ 4 calls/instance. A 13-instance fleet runs ≈ 52 codify calls/day at this default.

---

## 8. Producers other than dream

Dream is the *consolidator* of commitments — not the only producer. Other producers continue to write directly to `state/commitments/*.yaml` per the commitments-and-reengage spec:

- **Live session (agent during conversation):** when the agent commits to a future act ("ti scrivo giovedì"), it writes the YAML inline.
- **Operator (`jc-commitments add`):** manual queueing.
- **Heartbeat tasks:** any builtin can queue.
- **Dream:** queues re-engagement touches (via `lib/reengage/queuer.py`), follow-ups inferred from open threads, and "verify-this" pings tied to staged SENSITIVE diffs ("operator hasn't approved diff X in 48h — surface it again").

All producers obey the same schema and the same engine ticks them.

---

## 9. Memory consolidation specifics

### 9.1 Duplicate detection

A "duplicate group" is two-or-more L2 entries whose `slug` differs but whose **title alias set + frontmatter `tags` set + first 200-char content hash** are within Jaccard 0.85. Detection runs against the FTS5 index built by `jc memory rebuild`, plus a content-similarity scan in `lib/dream/consolidate.py`.

Resolution policy:
- **Auto-merge (LOW risk)** if both entries have `state: draft` and one has `last_verified` empty. Keep the older `slug`, fold content from the newer.
- **Stage (SENSITIVE)** otherwise. Operator decides which to keep.

### 9.2 Contradiction detection

Two L2 entries link the same `[[entity]]` but assert mutually-exclusive facts (e.g. one says `state: active`, another references it as `retired`). Detected via backlink graph + frontmatter scan; resolution is always staged — never auto.

### 9.3 Backlink integrity

A `[[wikilink]]` whose target slug does not exist in the index. Resolution: auto-create a stub entry under `memory/L2/stubs/<slug>.md` with the link's surrounding sentence as the body, `state: stub`. Logged in report. (LOW.)

### 9.4 Stale timestamps

An L2 entry whose `last_verified` is > 90 days old AND whose contents reference the live world (heuristic: contains a date, a price, a name with `state: active`). Resolution: emit a `verify-this` commitment due in the next 07:00 / 19:00 slot. (MEDIUM — a touch, not a content change.)

---

## 10. Coexistence with `lib/self_model/`

`lib/self_model/` is currently invoked manually (via `jc self-model run-now` or test harness). It stays as a library; `lib/dream/consolidate.py` and `lib/dream/codify.py` import its detectors and proposer.

Migration:

- **Phase A (this PR + implementation):** dream wraps self_model. Both work side-by-side. `jc self-model run-now` continues to exist as a debugging tool.
- **Phase B (next release):** the `jc self-model run-now` shim becomes a thin alias to `jc-dream tick --phases consolidate,codify --no-apply`. Behavior is identical; CLI surface keeps backward compat.
- **Phase C (~2 releases out):** if no operator uses the shim, drop it.

No code changes to `lib/self_model/` in this PR. The detector / proposer / applier surface is treated as fixed contract by `lib/dream/`.

---

## 11. Sub-spec: Commitments & Re-engagement

The companion `docs/specs/commitments-and-reengage.md` defines:
- `jc-commitments` engine (binary + module + heartbeat builtin)
- `lib/commitments/{schema,engine,actions}.py`
- YAML schema for `state/commitments/<slug>.yaml`
- `jc-reengage` silence detector (`lib/reengage/{conf,detector,queuer}.py`)
- Migration from Mario's `ops/commitments-tick.py`

**Status under this umbrella:** the commitments-and-reengage sub-spec is finalized except for the open questions below. Dream emits commitments via the same schema; reengage emits via the same schema. No new commitment file format is introduced by this spec.

Dream uses `lib/reengage/queuer.py` as one of its commitment emitters (see §3, §5.3). The reengage module ships in this PR as part of the sub-spec; dream consumes it.

---

## 12. Out of scope (v1)

- **Brain-call-at-dispatch** for re-engagement touch text. v1 = templates required (sub-spec).
- **Cross-instance dreams.** A "fleet dream" that learns patterns across all 13 instances is out of scope. Each instance dreams independently. Cross-instance learnings move via `jc memory export` + manual review.
- **Vector-DB-backed consolidation.** Current Jaccard + FTS5 is sufficient up to ~10k L2 entries/instance. Revisit if any instance crosses 5k.
- **Web UI / dashboard** for dream review. CLI only in v1.
- **Approval via in-chat DM.** Currently operator approves via `jc-dream approve` from a shell. In-chat approval is appealing but introduces a Zone-4 surface (binding action over chat) — defer.
- **Model fine-tuning.** Dream changes memory and rules, not weights. Same as Anthropic Dreams.

---

## 13. Privacy & security

- **Dream runs entirely local.** No data leaves the instance except whatever the codifier brain call sends (same surface as any other LLM call in JC).
- **Transcripts are read-only** to dream. Gateway remains the only writer.
- **No transcript content** is written into `state/dreams/<utc>.md` except in `### Source dreams` references — which carry the `dream_id`, not the verbatim quote. Quotes that need preserving go into the relevant playbook or learning entry, which the operator reviews when it lands.
- **DKIM gate** (existing surface in `lib/self_model/applier.py`) is the apply-time defense for SENSITIVE diffs. Dream does not relax it.
- **No erotic/sexual content persists** (existing RULES rule). Dream's classifier auto-rejects any candidate playbook / learning whose source transcript contains material flagged by the existing content-sensitivity filter; rejection logged with diff_id, content not written to disk.

---

## 14. Test plan

### 14.1 Unit
- `lib/dream/reflect.py` — window arithmetic, transcript-delta computation, byte-offset correctness across resumed runs.
- `lib/dream/consolidate.py` — duplicate detection (synthetic L2 set), contradiction detection, backlink integrity, stale-timestamp logic.
- `lib/dream/codify.py` — emitter selection, sealed-sum-type correctness for `ProposedArtifact`.
- `lib/dream/risk.py` — risk classification matrix coverage; IMMUTABILE / FROZEN guard.
- `lib/dream/apply.py` — auto-apply LOW, stage SENSITIVE, reject IMMUTABILE; soft-retain rollback.
- `lib/dream/emitters/playbook.py` — schema correctness, slug collision resolution.
- `lib/dream/emitters/commitment.py` — produces valid YAML accepted by `lib/commitments/schema.py`.

### 14.2 Integration
- **Synthetic dream:** prepare a fixture instance with 3 days of transcripts, run `jc-dream tick`, assert: 1 playbook created (MEDIUM auto), 1 broken-backlink stub created (LOW auto), 1 RULES proposal staged (SENSITIVE), 1 commitment queued, report file written.
- **Replay safety:** run the same fixture twice. Second run must produce zero new artifacts (idempotency via `diff_id` dedup against `state/dreams/*.md`).
- **Reject rollback:** auto-apply a MEDIUM diff, then `jc-dream reject <id>` within 24h, assert the file is reverted.
- **Approval flow:** stage a SENSITIVE RULES diff, `jc-dream approve <id>`, assert RULES.md updated and `applier` invoked correctly.

### 14.3 Soak
- 7-day soak on a non-prod instance: dream runs nightly, no growth in `state/dreams/` content size beyond ~50KB/day, no spurious SENSITIVE diffs, operator inbox not flooded.

### 14.4 Approval / DKIM
- IDENTITY.md and IMMUTABILE RULES sections: dream **must** stage, **must not** auto-apply. Test asserts via fixture targeting `§1 TRUST MODEL`.

---

## 15. Migration

1. **Phase A (this PR):** specs only. No runtime changes.
2. **Phase B (commitments PR):** ship `lib/commitments/` + `lib/reengage/` + scaffold `state/commitments/`. Existing Mario instance gets the commitments/re-engage scaffold through the `2026.05.12.01` release hook run by `jc update`.
3. **Phase C (dream PR):** ship `lib/dream/` + `jc-dream` binary + `state/dreams/` scaffold. Heartbeat builtin disabled by default. Operator enables per instance.
4. **Phase D (default-on, ~3 releases later):** once we have stable adoption signal, flip `dream_tick` default to `enabled: true` in `jc-init` templates.

`lib/self_model/` is touched zero times across all phases.

---

## 16. Open questions

1. **Brain choice for codify.** Default to `claude:opus` for quality, fall back to `claude:sonnet` on opus failure, or always sonnet for cost? Recommendation: opus default, sonnet fallback. Rationale: nightly cadence + 13 instances = 13 opus runs/night ≈ negligible.
2. **Playbook retrieval mechanism.** Should playbooks be auto-loaded at session start if their `trigger` matches the inbound message? Or operator-pinned in HOT.md? Recommendation: ship both — frontmatter `trigger` enables auto-retrieval, but HOT.md can pin a playbook explicitly.
3. **L3 vs L2 placement.** Should playbooks live in a new `memory/L3/playbooks/` or under existing `memory/L2/playbooks/`? Recommendation: L2. Introducing L3 just for playbooks is over-engineering; the layer distinction is "always loaded vs on-demand", and playbooks are on-demand.
4. **Approval channel.** Sub-spec for `jc-dream approve` UX — Telegram message + reply-to-confirm? Email with DKIM? Both? Recommendation: shell-only in v1, defer richer channels.
5. **Dream conflict with live session.** What if `jc-dream tick` runs while the user is mid-conversation? Recommendation: dream lease respects `state/gateway/lease.lock` — if gateway holds the lease and recent transcript activity is < 5 min old, dream defers 60 min and retries.
6. **Multi-day batched dreams.** If an operator disables dream for a week then re-enables, should it dream over the full backlog (one large dream) or chunked nightly catch-up? Recommendation: chunked — `window` capped at 48h per tick; runner schedules itself for the next slot until caught up.
7. **Anthropic Dreams API integration.** Anthropic ships a managed `Dreams` endpoint. Use it directly (cheaper, faster) or always run local pipeline (no vendor lock-in)? Recommendation: local pipeline is canonical; the Anthropic Dreams endpoint becomes one possible backend for the `codify` phase (config `dream.codify_backend: local | anthropic-managed`). Same Reflection → Codify boundary, two impls.

---

## 17. Sub-spec coordination — open questions inherited

From `commitments-and-reengage.md` §Open questions, the umbrella tracks these as in-scope for the unified PR:

- Touch text source (templates v. brain-at-dispatch) → resolved here: v1 templates only.
- Sweep runner — confirmed out of scope; separate spec.
- Gateway-side reset hook — recommendation: ship in v1 (already in sub-spec implementation order).
- Per-tracked-chat allowed-slots — recommendation: ship per-chat override field in `ops/reengage.yaml`; default to global.
- Failed-touch escalation — recommendation: Telegram alert to operator on first failed touch, then suppress duplicates per silence-episode.

---

## 18. Implementation order

This umbrella does **not** ship code. It frames the implementation order across the two implementation PRs that follow:

**Implementation PR #1 — Commitments & Re-engagement** (per sub-spec §Implementation order)
1. `jc-commitments` engine + schema + `telegram-send` dispatcher + heartbeat builtin
2. `jc-init` updates: scaffold `state/commitments/{,done/,failed/}`, disabled `commitments_tick` task
3. Migration script for Mario
4. Test coverage
5. `jc-reengage` builtin + config + detector
6. Gateway-side cancellation hook
7. RULES.md §24/§25/§26 template additions

**Implementation PR #2 — Dream pipeline**
1. `lib/dream/schema.py` — data model only, no orchestration
2. `lib/dream/reflect.py` + tests
3. `lib/dream/risk.py` + tests
4. `lib/dream/consolidate.py` (wraps self_model.detector + new checks) + tests
5. `lib/dream/emitters/{playbook,learning,commitment}.py` + tests
6. `lib/dream/codify.py` (wraps self_model.proposer + new emitters) + tests
7. `lib/dream/apply.py` (auto-apply LOW/MEDIUM, stage SENSITIVE) + tests
8. `lib/dream/report.py` + dream-report rendering
9. `bin/jc-dream` CLI + `lib/dream/cli.py`
10. `lib/heartbeat/builtins/dream_tick.py`
11. `jc-init` updates: scaffold `state/dreams/`, `memory/L2/playbooks/`, disabled `dream_tick` task
12. Integration tests, soak

PRs #1 and #2 are independent — #2 imports from #1 (`lib/reengage/queuer` and the commitments schema), but does not require #1 to be merged first; both can develop on parallel branches with `lib/commitments/` and `lib/reengage/` mocked in #2 until #1 lands.

---

## 19. Success criteria (post-launch)

Quantitative — measured 14 days after `dream_tick` is enabled on the first 3 instances (Rachel, Florian, Mario):

- **Memory hygiene:** broken backlinks count drops to 0 within 7 days and stays there. Duplicate-entry rate drops > 75%.
- **Re-engagement:** at least one re-engagement commitment fires correctly within 7 days; zero false positives (touches firing while user is active).
- **Playbook adoption:** at least 5 active playbooks per instance after 30 days; agent references a playbook in conversation at least 1×/week.
- **Operator approval queue:** SENSITIVE diffs stage ≤ 3 / instance / week. If higher, codify is too aggressive and risk thresholds need tuning.
- **No runaway:** zero auto-apply rollbacks beyond the soft-retain window. Zero IMMUTABILE bypass attempts.

Qualitative — operator confirms after 30 days that:

- Morning briefings are noticeably sharper (playbooks feeding context).
- Less manual memory cleanup (consolidation eating the chore).
- Re-engagement touches read on-brand (not robotic), reset cleanly on inbound.

If two or more quantitative criteria fail at 14 days, halt by setting `dream_tick: enabled: false` on the affected instance and re-tune before re-enabling.

---

## 20. Decision log (this spec)

- **Name:** "Dream" chosen over "Reflection" / "Consolidate" / "Sleep cycle". Rationale: matches Anthropic's term, lower cognitive overhead for new operators encountering both.
- **`lib/self_model/` kept:** rename would generate diff-noise with no behavior change. The dream pipeline wraps it.
- **Playbooks in L2, not L3:** L1/L2 distinction is "always-loaded vs on-demand"; playbooks are on-demand. New layer is unjustified.
- **Default schedule 03:30:** off-peak, after quiet hours, before 07:00 briefing window.
- **Risk classifier separate from frozen-section guard:** defense-in-depth. Risk class decides apply path; frozen-section guard is the no-bypass invariant.
- **One umbrella spec + sub-spec(s):** chose this over rewriting commitments-and-reengage into one mega-spec. Sub-specs stay focused and reviewable; umbrella does coordination.

---

## 21. References

- Anthropic Dreams (Claude Managed Agents) — research preview, May 2026: <https://platform.claude.com/docs/en/managed-agents/dreams>
- VentureBeat coverage: <https://venturebeat.com/technology/anthropic-introduces-dreaming-a-system-that-lets-ai-agents-learn-from-their-own-mistakes>
- SiliconANGLE coverage (operator scenarios): <https://siliconangle.com/2026/05/06/anthropic-letting-claude-agents-dream-dont-sleep-job/>
- `docs/specs/commitments-and-reengage.md` — sub-spec, ships with this umbrella.
- `lib/self_model/` — existing pattern-detection engine the dream pipeline wraps.
- `lib/heartbeat/` — runtime that schedules the dream tick.
- RULES.md `§24 RE-ENGAGEMENT` (Rachel instance) — content-side discipline for the touches the reengage subsystem queues.
