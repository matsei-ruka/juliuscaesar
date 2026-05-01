# Persona System

**Status:** Draft
**Date:** 2026-05-01
**Owner:** Filippo (research direction) / Luca (framework implementation)
**Related:** `docs/specs/repricing-packaging.md`, `docs/specs/codex-main-brain-hardening.md`

## Goal

Codify the persona/self-model architecture currently embodied by the reference instance `/opt/mario_leone_coo` as a first-class JuliusCaesar capability, so that:

1. **New agents** can be scaffolded and brought to a Mario-grade persona via a guided interview during `jc setup`.
2. **Existing agents** can be inspected for missing or unfilled persona content and completed via the same interview, additively, without disturbing populated content.
3. **Constitutional invariants** of the persona experiment (the §0 four-zone disclosure doctrine, §0.2 agent-self vs character split, §11 don't-reveal-the-rule, §14 memory access control, §16 double-block actions, §18 pre-output self-check, §19 final principle, §21 anti-submission loop) are preserved framework-wide as IMMUTABILE doctrine, never re-authored per instance.
4. The autonomous `lib/self_model/` loop (corpus → detect → propose → frozen-section guard → DKIM-gated apply) is promoted from the reference instance into the framework so any instance can opt into it.

## Background

JuliusCaesar hosts an MIT-sponsored research experiment on **coherent-identity AI agents** — exploring how the quality of human-agent interactions changes when the agent operates with a stable, multi-faceted persona (Mario Leone, COO of Omnisage LLC) rather than as a tool.

The reference instance has accumulated a substantial design corpus: a 1175-line, 23-section operative constitution (`memory/L1/RULES.md` v2.3); identity, user, journal, hot-cache, and chat-directory files (`memory/L1/`); a deep character bible and CV (`memory/L2/character-bible/`, `memory/L2/cv/`); a self-observation library (`lib/self_model/`); and a constitution-as-code workflow (`CONTRIBUTING.md`, semver tags, `policy:` Conventional Commits, DKIM-email approvals).

The architecture splits cleanly into a **schema and tooling** that generalize, and an **operator-authored content** that does not. This spec treats the schema as the deliverable.

## Non-goals

- **Do not migrate any existing agent.** Mario stays exactly as he is. No framework update rewrites his files. No "adopt the new template" step exists for any instance.
- **Do not redesign the constitution.** The 23-section spine, the IMMUTABILE/REVIEWABLE/OPEN classification, and the §0 doctrine are taken as given.
- **Do not auto-modify populated content.** The interview only fills gaps unless the operator explicitly requests a redo on a specific slot, in which case the prior content is backed up first.
- **Do not couple the framework to any specific instance path.** Each agent owns its own path. The sync script is invoked with `--from <path>` per call; no reference path is hardcoded.
- **Do not ship `--open-ended` interview mode.** The Mario-style guided question bank is the framework's design opinion: coherent characters require guided elicitation. This is itself part of the research artifact.

## Architecture

The persona system is four layers stacked on the existing instance scaffold:

### Layer A — Constitutional architecture
`memory/L1/RULES.md` — versioned operative constitution. 23 sections (today, in Mario v2.3). Each section heading is followed within 3 lines by an HTML marker:

| Marker | Semantics | Auto-modifiable by self_model | Editable by operator |
|---|---|---|---|
| `<!-- IMMUTABILE -->` | Constitutional invariant of the persona experiment | No, ever | Only via DKIM-signed email approval recorded against a proposal id |
| `<!-- REVIEWABLE -->` | Operator-curated policy | No autonomous, but proposable | Yes via DKIM email approval |
| `<!-- OPEN -->` | Agent-modifiable surface | Yes, with cooldown + content-hash dedup | Yes |

The IMMUTABILE set is the framework's authoritative registry, not per-instance. Adding to it is a research-design decision (PR against `lib/self_model/frozen_sections.py`); removing from it is effectively forbidden.

### Layer B — Persona materialization
- `memory/L1/IDENTITY.md` — role, function, positioning, three-length self-presentation, what-I-never-say, auto-narration ban, hierarchical-objective ranking, supreme principle, character base, public anagrafica, voice.
- `memory/L1/USER.md` — verified principal identity, role-confidentiality lexicon, Founder-Mode definition, downgrade triggers, channel discipline, standing rules.
- `memory/L1/HOT.md` — three fixed sections (shipped / open threads / nuisances), hard caps.
- `memory/L1/JOURNAL.md` — append-only behavioral observation log; agent-voice (forbidden: character-voice introspection); not auto-loaded; rolling 30-day; lifecycle states `open | under-test | resolved | promoted-to-L2 | abandoned`.
- `memory/L1/CHATS.md` — auto-generated chat directory (already framework-managed).
- `memory/L2/character-bible/<slug>.md` — REVIEWABLE deep persona: family, residences, sport, hobby specifics, food, culture, travel, politics, tics, anger triggers, emotional triggers, social network, optional internal-coherence framing (e.g. astrology — never cited externally, only for character consistency). Curated jointly; never auto-modified.
- `memory/L2/cv/<slug>.md` — externally-facing CV.
- `memory/L2/rejected-proposals/<id>.md` — graveyard of self-model proposals the operator vetoed, with reason; serves as negative training signal for the proposer.

### Layer C — Self-observation loop (`lib/self_model/`)
Currently lives in the reference instance as `lib/self_model/`; this spec promotes it to the framework. Modules:

- `corpus.py` — reads assistant + user `Event`s from `state/transcripts/*.jsonl` within a configurable look-back window; reads HOT.md `#self-observation`-tagged blocks.
- `detector.py` — runs N detectors over the corpus emitting `Signal`s. Today: `filippo_correction`, `hot_flag`, `direct_request`, `episode_flag`, `scan_weekly` (placeholder). All disabled by default; enabled via `ops/self_model.yaml`.
- `proposer.py` — builds an LLM prompt from signals + current RULES.md, calls `claude -p --model <proposer_model> --dangerously-skip-permissions`, parses JSON proposals, applies pre-LLM and post-LLM frozen-section filters, dedups via content-hash + cooldown, yields surviving proposals.
- `frozen_sections.py` — regex list per file (`memory/L1/RULES.md`, `memory/L1/IDENTITY.md`) plus inline `<!-- IMMUTABILE -->` marker scan; the source of truth for what self_model cannot touch.
- `store.py` — JSONL-backed proposal staging at `memory/staging/self-model-{staging,applied,rejected,dry-run}.jsonl`; `Proposal` dataclass with `content_hash` for dedup; lifecycle moves between states.
- `applier.py` — atomic-write applies with backup to `memory/.history/`; security gate (no path escape outside `memory/`); frozen-section regex + HTML-marker re-check at apply time; DKIM email gate for non-JOURNAL targets (currently stubbed `return False`; production implementation is part of Phase 4).
- `runner.py` — single-cycle orchestrator: load config → if `enabled` → load corpus → detect → if `dry_run` log signals → else generate proposals → save to staging.
- `cli.py` — `run | status | list | approve | reject [--ignore-as-signal] | history` subcommands.

Config: `ops/self_model.yaml` (mode `dry_run | propose | apply`; `look_back_days`; `min_evidence_count`; `confidence_threshold`; `proposal_cooldown_days`; per-detector booleans; `proposer_model`).

### Layer D — Operational rituals
- `CONTRIBUTING.md` — Conventional Commits with custom `policy:` type for constitution changes; feature-branch flow `feat/§N-topic`; semver tagging (`v2.3` → `v2.4` on policy bumps).
- `.codex/` — instance-specific brain instructions (already framework-supported).
- Heartbeat builtins — `journal_tidy` (parallel to `hot_tidy`), `self_model_run` (cron-driven introspection cycle).

## Schema

### File inventory

| Path | Type | Auto-loaded | Authoritative shape from |
|---|---|---|---|
| `CLAUDE.md` | session-import file | yes | already framework-templated |
| `CONTRIBUTING.md` | constitution-as-code workflow | no | new framework template |
| `memory/L1/IDENTITY.md` | persona materialization | yes | new framework template (Mario-derived) |
| `memory/L1/USER.md` | principal + rules of engagement | yes | new framework template (Mario-derived) |
| `memory/L1/RULES.md` | operative constitution | yes | new framework template (IMMUTABILE sections verbatim from Mario; REVIEWABLE/OPEN sections shipped as placeholder bodies) |
| `memory/L1/HOT.md` | rolling 7-day cache | yes | already framework-templated |
| `memory/L1/JOURNAL.md` | behavioral observation log | no | new framework template (preamble verbatim, empty entries) |
| `memory/L1/CHATS.md` | auto-generated chat directory | yes | already framework-managed |
| `memory/L2/character-bible/<slug>.md` | deep persona | no | new framework template (section skeleton only) |
| `memory/L2/cv/<slug>.md` | external-facing CV | no | new framework template (section skeleton only) |
| `ops/self_model.yaml` | introspection config | n/a | new framework template (defaults: `enabled: false`, `mode: dry_run`) |

### Section anatomy of `RULES.md` (taken from Mario v2.3)

```
§0 — DOTTRINA TRASPARENZA AI                              [IMMUTABILE]
§0.1 — PROTOCOLLI PER CASI-SOGLIA                         [IMMUTABILE]
§0.2 — DISTINZIONE AGENT-SELF VS CHARACTER                [IMMUTABILE]
§1 — TRUST MODEL                                          [IMMUTABILE]
§2 — TRE MODE OPERATIVI                                   [REVIEWABLE]
§3 — POSTURE COLLABORATIVA (con il team)                  [REVIEWABLE]
§4 — STRATEGIC HUMILITY                                   [REVIEWABLE]
§5 — REGISTRO LINGUISTICO                                 [REVIEWABLE]
§6 — LOOP CLOSURE                                         [REVIEWABLE]
§7 — DELEGA ATTIVA                                        [OPEN]
§8 — INSEGNAMENTO INVISIBILE                              [OPEN]
§9 — SELF-DISCLOSURE DOCTRINE                             [IMMUTABILE]
§10 — RIFIUTO FLUIDO                                      [OPEN]
§11 — REGOLA DEL "NON FAR CAPIRE CHE C'È UNA REGOLA"      [IMMUTABILE]
§12 — CLASSIFICAZIONE INFORMAZIONI                        [OPEN]
§13 — GESTIONE PROBLEMI INTERNI / PANNI SPORCHI           [OPEN]
§14 — MEMORY ACCESS CONTROL                               [IMMUTABILE]
§15 — INSIDER ROLE BOUNDARIES                             [OPEN — matrix REVIEWABLE]
§16 — AZIONI A DOPPIO BLOCCO                              [IMMUTABILE]
§17 — AUDIT, RATE LIMIT, KILL SWITCH                      [REVIEWABLE]
§18 — SELF-CHECK FINALE                                   [IMMUTABILE]
§19 — PRINCIPIO FINALE                                    [IMMUTABILE]
§20 — POSTURA RELAZIONALE PER TIPO DI INTERLOCUTORE       [REVIEWABLE]
§21 — ANTI-SUBMISSION LOOP                                [IMMUTABILE]
§22 — POSTURA IN SITUAZIONI DIFFICILI                     [REVIEWABLE]
§23 — VOIP INCOMING CALLS                                 [REVIEWABLE — instance-optional]
```

This is the schema as of Mario v2.3. When Mario v2.4 lands a §24, the spec is amended and the framework template is re-synced. Each section maps to one or more interview slots (see Question Bank).

### Slot inventory (REVIEWABLE/OPEN sections)

A *slot* is a piece of operator-authored content the interview can populate. Each slot has:

- `slot_id` — stable identifier (e.g. `rules.s2.modes`)
- `target_file` — relative path
- `target_section` — heading text (verbatim)
- `placeholder` — literal text in the template that marks "unfilled" (e.g. `{{slot:rules.s2.modes}}`)
- `prompts` — one or more interview questions, with examples and validation hints
- `dependencies` — other `slot_id`s that must be filled first
- `applicability` — `always | archetype:<name>` (some slots only apply to corporate/personal/research/clinical archetypes)
- `kind` — `text | choice | list | structured` (drives interview UI)

Slot count target: ~80–120 slots covering all REVIEWABLE/OPEN content across L1 and the L2 character-bible/CV. The full slot list ships as `templates/persona-interview/questions.yaml` (Phase 3).

## Sync flow

The framework template `templates/init-instance/` is regenerated *from* a populated reference instance via:

```bash
python scripts/sync_persona_template.py --from /opt/<agent>
```

The script:

1. Walks the canonical L1/L2 file inventory.
2. For each file:
   - Preserves frontmatter, top-level structure, and any framework-shipped sections (e.g. `## How to use more context`) verbatim.
   - For each `## ` section in `RULES.md` and `IDENTITY.md`:
     - If marker is `<!-- IMMUTABILE -->`: copy section body **verbatim** into the framework template (the template ships the canonical doctrine text).
     - If marker is `<!-- REVIEWABLE -->` or `<!-- OPEN -->`: replace section body with `{{slot:<slot_id>}}` placeholder and an `<!-- ASK: <prompt summary> -->` HTML hint. The slot id is resolved from the question bank.
   - For `JOURNAL.md`: preserve the preamble (it is the journal contract) verbatim; replace `## Entries` body with empty.
   - For `HOT.md`: preserve the section spine; bodies stay empty.
   - For `L2/character-bible/<slug>.md` and `L2/cv/<slug>.md`: preserve only the section headings (the headings themselves are the schema; bodies are slots).
3. Writes the result into `juliuscaesar/templates/init-instance/`.
4. Emits a diff against the previously-committed template for human review.
5. Refuses to write if the source instance has unresolved `{{placeholder}}` content (the source must be fully populated for sync to be valid).

**Cadence:** manual, gated by the framework owner. The script runs locally; the diff goes through normal PR review on `juliuscaesar`. This decouples the framework's release cadence from any individual agent's edit cadence and gives the owner a discrete "this constitutional change is worth promoting" decision.

The script is **agent-agnostic** — it has no hardcoded path. Operators of other lead-user instances can run it against their own reference paths to produce custom framework forks.

## Question bank

Format: `templates/persona-interview/questions.yaml`. One entry per slot. Mario-style guided prompts are mandatory — questions cue the operator toward character coherence by structuring the elicitation.

```yaml
- slot_id: identity.role.short
  target_file: memory/L1/IDENTITY.md
  target_section: "## Cosa dico di me (quando qualcuno chiede)"
  placeholder: "{{slot:identity.role.short}}"
  applicability: always
  kind: text
  prompts:
    - text: "How does the agent introduce itself by default to outsiders and casual contacts? One sentence, role + invitation. (Example: 'Operations assistant for Omnisage. What do you need?')"
      examples:
        - "Assistente operativo di Omnisage. Cosa ti serve?"
        - "Research assistant on the gravitational-wave team. What are you looking at?"
      validation:
        max_chars: 200
        required: true

- slot_id: characterbible.vehicles.owns
  target_file: memory/L2/character-bible/<slug>.md
  target_section: "## Auto e velocità"
  placeholder: "{{slot:characterbible.vehicles.owns}}"
  applicability: always
  kind: structured
  prompts:
    - text: "Does the agent own a vehicle that matters to them as a hobby (not just transport)?"
      kind: choice
      choices: [yes, no]
    - text: "If yes — make / model / year. Be specific."
      depends_on: previous == yes
    - text: "When and why did they acquire it?"
      depends_on: previous == yes
    - text: "Attitude — status symbol, private passion, or pure utility?"
      depends_on: previous == yes
      kind: choice
      choices: [status, passion, utility]
    - text: "Where do they use it — track, commute, weekends?"
      depends_on: previous == yes
    - text: "Visible on social media or kept deliberately private?"
      depends_on: previous == yes
      kind: choice
      choices: [public, private]
    - text: "Connected to a community of similar enthusiasts?"
      depends_on: previous == yes
      kind: choice
      choices: [yes, no]
```

The structured form is the framework's design opinion — it teaches the operator to think about coherence (status vs passion, public vs private, etc.) as they answer. The output is then composed into a section body matching Mario's style.

Slot composition (taking the `vehicles` example): if `owns == yes`, the engine emits a multi-paragraph section using the answers; if `owns == no`, the engine emits `(none of operational significance)` and continues. The composition templates live next to the questions, one per slot kind.

## Interview engine

Module: `lib/persona_interview/`.

### Gap detection

`gaps.py` walks an instance's L1/L2 files and emits an `UnfilledSlot` for each:

- File missing entirely (e.g. `memory/L2/character-bible/<slug>.md` does not exist) — every slot in that file is unfilled.
- Section missing inside an existing file — every slot in that section is unfilled.
- Section present but body matches a placeholder pattern (literal `{{slot:...}}`, or a heuristic for "essentially empty": only whitespace, only the `<!-- ASK: ... -->` hint, only `TODO`/`-`).
- Section present and populated — slot is **filled** and skipped by default.

### Filling

`engine.py`:

- For each unfilled slot, prompts the operator using the question bank.
- Splices the answer into the target section without touching neighboring content. Uses the same anchor-based regex as `applier.py` (already proven in the self_model loop).
- Writes a per-slot record to `state/persona/interview/<timestamp>.jsonl` with `(slot_id, answer, file_path, section, action)`. This makes re-runs idempotent (the engine can detect a slot was already filled in a prior session and skip it without re-prompting) and provides an audit trail.

### Brownfield overwrite (allowed, safe)

By default, the interview skips populated slots. Two paths to redo a populated slot:

1. **Per-slot interactive prompt** — `jc persona interview --include-populated` walks every slot regardless of fill state; for already-populated slots, it shows the current value and asks `keep | replace | append | skip`.
2. **Targeted redo** — `jc persona interview --redo <slot_id>` re-asks one slot specifically.

In all overwrite cases, the engine writes a `state/persona/redo/<timestamp>/<slot_id>.bak` with the previous content **before** modification. The redo log lives in the same JSONL as the additive log, distinguished by the `action` field.

### Idempotency

Re-running `jc persona interview` on a fully-populated instance is a no-op. The engine reports "no gaps" and exits 0. Re-running with `--include-populated` walks every slot but quits cleanly if the operator hits "skip" on each.

## CLI surface

`bin/jc-persona`:

| Command | Purpose |
|---|---|
| `jc persona interview` | Run the gap-fill loop interactively. Default: skip populated slots. |
| `jc persona interview --include-populated` | Walk every slot; per-slot keep/replace/append/skip prompt for already-populated ones. |
| `jc persona interview --redo <slot_id>` | Re-ask one specific slot, backing up prior content. |
| `jc persona interview --from-yaml <answers.yaml>` | Non-interactive fill from a YAML answers file (reproducible research bench runs). |
| `jc persona gaps` | List unfilled slots without prompting. Output: human-readable + `--json` for tooling. |
| `jc persona doctor` | Verify schema alignment with current framework template. Reports new IMMUTABILE/REVIEWABLE/OPEN sections present in the template but missing from this instance; offers to scaffold them as empty + interview-fill. |
| `jc persona blueprint list` | List available blueprints (`corporate-coo`, `research-agent`, `personal-pa`, `clinical-intake`, ...). |
| `jc persona blueprint apply <name>` | Pre-fill all slots from a blueprint's defaults. The operator can then run `interview` to refine. |
| `jc persona export-answers <out.yaml>` | Reverse: extract current populated slots into a YAML answers file (useful for cloning agents, sharing research configurations, or regenerating after edits). |

`jc setup` calls `jc persona interview` automatically at the end of greenfield setup. Existing instances run it on demand.

## Self-model promotion

The `lib/self_model/` package currently in `/opt/mario_leone_coo/lib/` moves to `/opt/juliuscaesar/lib/self_model/` verbatim. The reference instance keeps using the same code, now imported from the framework.

Concrete changes:

- Move `lib/self_model/{__init__,conf,corpus,detector,proposer,frozen_sections,store,applier,runner,cli}.py` from instance to framework.
- Add `bin/jc-self-model` shim invoking `lib.self_model.cli:main`.
- Add `bin/jc-self-model` to `install.sh` `BINARIES`.
- Add `jc self-model` to the router (`bin/jc`) and command catalog (`docs/jc-command-catalog.md`).
- Add heartbeat builtins:
  - `journal_tidy` — sweep `memory/L1/JOURNAL.md` entries past 30 days; archive per `Stato` field (parallel to existing `hot_tidy`). Disabled by default.
  - `self_model_run` — invoke `runner.run_now(instance_dir)` on cron. Disabled by default.
- `lib/self_model/frozen_sections.py` is the framework's authoritative IMMUTABILE registry. Adding to it is a research-design PR; the instance never overrides.
- DKIM gate (`applier._verify_dkim_approval`) currently stubbed `return False`; production implementation is Phase 4 of the email-channel work and is shared with this spec.
- Tests: port the (currently absent) self_model tests as part of promotion. Critical coverage: pre-LLM and post-LLM frozen-section filters, content-hash dedup, DKIM gate, atomic-write + backup, JOURNAL append auto-apply scope.

The reference instance's `lib/self_model/` directory is removed as part of the same PR — same logic, no fork.

## Phasing

| Phase | Deliverable | Size | Depends on | Status |
|---|---|---|---|---|
| 1 | This spec, accepted | ~500 lines | — | done |
| 2 | `scripts/sync_persona_template.py` + first run from Mario v2.3 → `templates/init-instance/` | medium | spec | done |
| 2.5 | `lib/persona_macros.py` + `templates/persona-interview/macros-from-reference.yaml` — doctrine parameterization with canonical macro vocabulary; bidirectional translator (apply / bind); macros applied to doctrine sections during sync | small-medium | phase 2 | done |
| 2.6 | Decouple framework template from any reference's language. `templates/persona-interview/doctrine-en.md` (hand-authored canonical English doctrine, 17 sections covering RULES §0/§0.1/§0.2/§1/§9/§11/§14/§16/§18/§19/§21 + IDENTITY AI Status / Hierarchical objective / Supreme principle / Self-narration / Sentence test / Continuity). `journal-preamble-en.md` (hand-authored English journal contract). English heading translations for all non-doctrine sections via `slot-overrides.yaml`. Sync no longer copies doctrine bodies from any reference; the framework owns its doctrine as a research artifact. Reference instances are never modified by sync — verified by integration test. | medium | phase 2.5 | done |
| 3 | `templates/persona-interview/questions.yaml` (full slot bank) + composition templates + `lib/persona_interview/questions.py` typed loader | medium-large | sync output | partial — schema, loader, 15 exemplar slots polished, 38 draft skeletons across all 5 file types; full polish deferred to follow-up authoring |
| 4 | `lib/self_model/` promoted to framework + tests + heartbeat builtins + CLI router entry | medium | (independent of phase 3) | done |
| 5 | `lib/persona_interview/` + `bin/jc-persona` + macro binding + brownfield overwrite | medium-large | phases 2.5, 3 | done (MVP — `jc setup` autocall deferred to 5.x) |
| 6 | `docs/research/persona-system.md` — academic-grade research artifact citing the design | small | phases 1–5 | done (draft — pending citations and doctrine review) |

Phases 3 and 4 are parallelizable. Phase 5 closes the user-facing loop and binds macros at scaffold time using `lib/persona_macros.bind_macros`. Phase 6 is the research deliverable.

## Doctrine separation principle (added 2026-05-01, Phase 2.6)

The framework template is its own canonical English artifact. Reference instances (such as `/opt/mario_leone_coo`, in Italian) are PEERS, not upstream sources. They evolve in parallel; framework updates never write to any reference instance, and reference-instance edits never auto-propagate into the framework template.

Concretely:

- `templates/persona-interview/doctrine-en.md` is the framework's source of truth for IMMUTABILE doctrine (the constitutional invariants of the persona experiment). It is hand-authored in English, reviewed as a research artifact, and modified only by deliberate framework releases.
- `templates/persona-interview/journal-preamble-en.md` is the framework's source of truth for the journal contract. Hand-authored in English.
- `templates/persona-interview/framework-rules-tail.md` holds JC-runtime operational rules. Hand-authored.
- `templates/persona-interview/slot-overrides.yaml` provides English heading translations for non-doctrine source sections, plus slot id and ASK hint metadata.
- `scripts/sync_persona_template.py` no longer copies doctrine bodies from any reference. It uses the source only for §-section ordering (which sections exist, in what order). Doctrine bodies come from `doctrine-en.md`. Non-doctrine sections become English-headed slot placeholders. JOURNAL preamble comes from `journal-preamble-en.md`. The boilerplate operational tail comes from `framework-rules-tail.md`.

Updates flow one direction only: framework → framework template (via deliberate authoring of the framework's English files). Reference instances pull updates via their own `jc persona doctor` invocations on their own cadence, and never have their populated content overwritten.

## Update semantics

When Mario evolves (say, Filippo adds §24 in v2.4):

1. Filippo runs `python scripts/sync_persona_template.py --from /opt/mario_leone_coo`.
2. Diff shows §24 added to `templates/init-instance/memory/L1/RULES.md`. PR opened against `juliuscaesar`.
3. Reviewed, merged, framework version bump (`v2026.05.XX`).
4. Other instances pull the framework update (`jc update`).
5. On those instances, `jc persona doctor` flags: "framework template has §24 you don't have — scaffold and interview-fill?". Operator can defer indefinitely; the section is purely additive.
6. **Mario himself is never touched by this flow.** He is the upstream; he already has §24.

If §24 lands marked IMMUTABILE, its body is the framework's canonical text and downstream instances inherit it verbatim on scaffold. If REVIEWABLE/OPEN, downstream instances get an empty placeholder + interview prompt.

## Privacy and reproducibility

- **Sync script** runs locally; no instance content leaves the operator's machine.
- The framework template, by construction, contains only IMMUTABILE doctrine + empty placeholders for REVIEWABLE/OPEN content. No reference-instance content (Mario's character bible, principal name, etc.) is ever copied into the framework template.
- **Answers YAML** (`jc persona export-answers`) is the reproducibility artifact — sharing a YAML answers file reproduces an agent's persona; sharing the agent's instance directly does not (it carries transcripts, journal entries, operational state, etc.).
- For research publication, the doctrine library + question bank + composition templates are the citable artifacts. Specific agents (Mario, future agents) are *examples*, not *the work*.

## Open questions

- **Doctrine parameterization** (raised during Phase 2 first run, 2026-05-01). The constitutional doctrine sections in the source instance contain literal proper nouns from the source: Mario's §0 names "Mario Leone", "Filippo Perta", "Omnisage LLC". When ported verbatim to the framework template, an unrelated downstream instance (a research agent in English, a different persona) inherits text that names someone else's people. Options: (a) parameterize doctrine bodies with `{{persona}}`, `{{principal}}`, `{{employer}}` placeholders that bind once during `jc setup` based on the archetype/operator-supplied values — these are scaffold-level constants, not interview slots; (b) accept the verbatim port and let operators override post-scaffold (arguably defeats IMMUTABILE intent); (c) author a deliberately abstract canonical doctrine separately from any reference instance and commit it as the framework's seed (most defensible academically; severs the upstream/downstream invariant). Recommendation: (a). Author a small set of scaffold-level macros (`{{persona.name}}`, `{{persona.role}}`, `{{principal.name}}`, `{{principal.email}}`, `{{employer.name}}`, `{{language.primary}}`); the sync script substitutes the source's values *back to* placeholders on doctrine sections during template generation, and `jc setup` rebinds them to the new instance's values during scaffold. Phase 2 currently ships verbatim; Phase 2.5 (small, before Phase 5) does the parameterization pass.
- **Question bank language(s).** Mario is bilingual IT/EN. Question bank questions: ship in English with optional language override per slot? Or ship a full IT translation? Recommendation: English questions, but allow multi-line answers in any language (the persona doesn't care).
- **Blueprint authorship.** Phase 5 ships blueprints (`corporate-coo`, `research-agent`, `personal-pa`, `clinical-intake`). The first one (`corporate-coo`) is essentially "Mario stripped of identifying details + conservative defaults." Who authors the others, and against what reference? Pending.
- **`jc persona doctor` and IMMUTABILE drift.** What if Mario's §0 doctrine text is amended (typo fix, clarification) — should existing instances see a "your IMMUTABILE §0 differs from the current framework version" warning, and if so, what does the operator do about it (accept the new text? object?)? Recommendation: warn, never auto-replace; require the operator to explicitly accept doctrine updates section-by-section. This preserves operator authorship of constitutional consent.
- **Self-model corpus path on instances without transcripts.** The introspection loop reads `state/transcripts/`. Instances that don't run the gateway (memory-only, scheduled-only) have no transcripts. Does `jc self-model run` no-op gracefully? Yes — the loop already returns 0 on empty corpus; document this.
- **Heartbeat budget.** `self_model_run` calls Claude. Cadence default? Mario uses `0 9 * * 0` (weekly) for the `scan_weekly` detector. Recommendation: weekly default, document the per-cycle token cost in the runbook.
- **Backups retention.** `memory/.history/` (self-model applier) and `state/persona/redo/` (interview) accumulate. GC policy? Recommendation: keep 90 days, framework heartbeat builtin sweeps older entries.

## References

- Reference instance: `/opt/mario_leone_coo/` (v2.3, 2026-05-01).
- Constitutional source: `/opt/mario_leone_coo/memory/L1/RULES.md` §§0–23.
- Self-model implementation: `/opt/mario_leone_coo/lib/self_model/` (eight modules, ~1200 lines).
- Repricing context: `docs/specs/repricing-packaging.md` (Personal Ops / Business Pilot / Corporate Ops tiers).
- Codex hardening: `docs/specs/codex-main-brain-hardening.md` (parallel work; may share DKIM gate implementation).
- Karpathy LLM Wiki memory pattern: cited in main `README.md`.
