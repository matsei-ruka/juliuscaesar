# Changelog

All notable changes to JuliusCaesar are documented here. Versions follow CalVer
(`YYYY.MM.DD`). Newest first.

## 2026.05.02.1

The **persona system** lands. JuliusCaesar can now host the full coherent-identity
agent architecture used by the lead-user reference (Mario Leone, Omnisage LLC):
constitutional doctrine with `IMMUTABILE` invariants, autonomous self-observation
with frozen-section guards and DKIM-gated apply, gap-driven interview engine
that builds an instance from a guided question bank.

Two new top-level subcommands ship: `jc persona` (interactive interview) and
`jc self-model` (autonomous self-observation loop). The framework template
`templates/init-instance/` is now in canonical English, fully macroed
(`{{persona.full_name}}`, `{{principal.name}}`, `{{employer.full_name}}`, …),
ready to be bound to a specific persona at scaffold time.

Companion docs: `docs/specs/persona-system.md` (engineering spec, 8 phases
including this one), `docs/research/persona-system.md` (academic-grade
write-up of the doctrine and the four-zone disclosure framework, draft),
`docs/self_model/FROZEN_SECTIONS_REFERENCE.md` (registry + rationale +
three-layer guard interaction), `docs/runbooks/self-model-go-live.md`
(first-time activation playbook with sha256 baseline, health checks,
pattern-OK-vs-red-flag table, kill switch).

### How to use this version

#### A. New agent (greenfield)

```sh
git clone https://github.com/matsei-ruka/juliuscaesar ~/juliuscaesar
cd ~/juliuscaesar && ./install.sh

jc init ~/my-new-agent              # scaffold the instance
cd ~/my-new-agent
jc persona interview                # bind macros + walk slot questions
jc doctor --fix                     # verify
jc gateway start                    # come online
```

`jc persona interview` does two things in sequence:

1. **Macro binding** — asks once for the persona's name, slug, role, principal's
   name + email, employer's name. The framework template's doctrine sections
   reference these via `{{macros}}`; binding rewrites every L1/L2 file with the
   concrete values.
2. **Slot interview** — walks every unfilled section (operating modes, postures,
   strategic humility, character bible, CV, etc.) with Mario-style guided prompts.
   Composition templates render the answers into clean markdown that splices
   into the right place. Brownfield-safe: re-runs only fill gaps, never overwrite.

#### B. Existing agent (upgrade)

```sh
cd ~/juliuscaesar
git pull && ./install.sh            # pick up new shims (jc-persona, jc-self-model)

cd <existing-agent-instance>
jc persona doctor                   # see which new sections the framework expects
jc persona gaps                     # list missing/unfilled slots
jc persona interview                # fill what's missing — won't touch populated content
```

Existing instances are **not** auto-modified by the upgrade. Operator decides what
to scaffold and when. The interview is gap-driven and idempotent: re-running it
on a fully-populated instance is a no-op.

If you want to enable the autonomous self-observation loop on an existing instance,
follow the runbook: `docs/runbooks/self-model-go-live.md`. It ships disabled
(`ops/self_model.yaml` `enabled: false`); the runbook walks an operator through a
12-hour supervised dry-run with explicit kill switch before promoting to
`mode: propose` or `apply`.

### Upgrade notes for existing instances

The framework auto-migrates where it can; the items below need an operator
action after `git pull && ./install.sh`.

1. **Re-run installer** — two new shims: `jc-persona`, `jc-self-model`.
   `./install.sh` installs them. `jc doctor` flags missing ones if the install
   was skipped.
2. **`jc persona interview` is opt-in.** Existing instances are unaffected.
   Run it when ready; it scans your L1/L2 files, finds anything that's missing
   or still has `{{slot:…}}` placeholders, and walks you through filling them.
   Re-runs are no-ops on populated content.
3. **`jc self-model` ships disabled.** Same as the email channel and user model
   from the previous release. Enable explicitly via `ops/self_model.yaml` (`enabled: true`,
   start in `mode: dry_run`); see `docs/runbooks/self-model-go-live.md`.
4. **Frozen-section registry expanded.** `lib/self_model/frozen_sections.py` now
   covers §9 (Self-disclosure doctrine), §17 (Audit/rate-limit/kill-switch),
   English aliases for every doctrine heading shipped by the framework template,
   and IDENTITY's `Auto-narrazione`/`Test della frase`/`CONTINUITY`/`Character`
   sections. If your instance has these sections, the self-model now refuses to
   propose changes to them (DKIM-only, as designed). No action needed for
   existing instances unless you have a `self_model` cycle in progress.
5. **Sub-section IMMUTABILE markers in sync.** The sync script (`scripts/sync_persona_template.py`)
   now detects nested `### Heading` + `<!-- IMMUTABILE -->` patterns inside
   otherwise-OPEN sections and pulls the matching English doctrine from
   `templates/persona-interview/doctrine-en.md`. The lead-user reference's `§15`
   pattern (`### Principio` IMMUTABILE inside an OPEN parent) is now properly
   rendered. No action needed for existing instances.

### Added

- **Persona system (Phases 2–7).** Full implementation of the coherent-identity
  agent architecture.
  - `bin/jc-persona`: gap-driven interview engine. Subcommands: `interview`
    (`--include-populated`, `--redo <slot_id>`), `gaps` (`--json`), `doctor`.
  - `bin/jc-self-model`: autonomous self-observation loop CLI. Subcommands:
    `run`, `status`, `list`, `approve`, `reject`, `history`.
  - `lib/persona_macros.py`: bidirectional macro translator (`apply_substitutions`
    / `bind_macros`) + `CANONICAL_MACROS` vocabulary contract (11 keys across
    `persona.*`, `principal.*`, `employer.*`).
  - `lib/persona_interview/`: gap detection, composition, splice (atomic write
    + backup to `state/persona/redo/`), engine orchestrator, terminal Prompter.
  - `lib/self_model/`: promoted from instance to framework. Three-layer
    guard (pre-LLM signal filter, post-LLM proposal filter, applier HTML
    marker re-check) + DKIM gate (currently fail-closed stub).
  - `templates/persona-interview/doctrine-en.md`: 18-section canonical English
    doctrine. Hand-authored as a research artifact; not derived from any
    reference instance. Macro-parameterized.
  - `templates/persona-interview/questions.yaml`: 53-slot question bank with
    schema documentation. 15 polished exemplars across all 5 file types
    (RULES, IDENTITY, USER, character-bible, cv); 38 draft skeletons for
    follow-up authoring.
  - `templates/persona-interview/slot-overrides.yaml`: English heading
    translations + ASK hints + slot id curation for every section.
  - `templates/persona-interview/macros-from-reference.yaml`: 12 substitutions
    used by sync to genericize source-instance proper nouns into macros.
  - `templates/persona-interview/framework-rules-tail.md`: framework operational
    rules tail, sync-safe location.
  - `templates/persona-interview/journal-preamble-en.md`: hand-authored English
    journal contract.
  - `scripts/sync_persona_template.py`: agent-agnostic template generator.
    `--from <path>` regenerates `templates/init-instance/` from a populated
    reference instance, preserving doctrine from `doctrine-en.md` and slotifying
    the rest with English headings. Idempotent. Refuses to sync sources with
    unresolved placeholders.
  - Heartbeat builtins: `journal_tidy` (rolling 30-day journal sweep), `self_model_run`
    (cron-driven self-observation cycle). Both ship disabled.
- **Documentation.**
  - `docs/specs/persona-system.md` — 13-section engineering spec, 8 phases
    tracked, open questions noted.
  - `docs/research/persona-system.md` — academic-grade research artifact (draft,
    pending citations + doctrine review).
  - `docs/self_model/FROZEN_SECTIONS_REFERENCE.md` — companion doc for the
    frozen-sections registry: which sections are protected, why, how the three
    guard layers interact.
  - `docs/runbooks/self-model-go-live.md` — first-time-activation playbook for
    the autonomous self-observation loop. Eight phases with health checks, OK
    vs red-flag pattern table, kill switch, recovery.
- **Tests.** 114 persona-system tests across 11 test files: sync, macros, frozen
  sections, store, applier, questions loader, gaps, compose, splice, engine.

### Changed

- **`templates/init-instance/`** regenerated as canonical English persona
  template. Files now contain:
  - 17 IMMUTABILE doctrine sections from `doctrine-en.md`
  - English-headed slot scaffolds with `{{slot:<id>}}` placeholders +
    `<!-- ASK: ... -->` hints for every operator-authored section
  - English JOURNAL preamble with the agent-voice contract
  - Character-bible + CV section skeletons
  - `ops/self_model.yaml` with all detectors disabled
  - `CONTRIBUTING.md` with the constitution-as-code workflow
- **`lib/self_model/frozen_sections.py`** registry expanded (12 → 25 RULES
  patterns; 7 → 22 IDENTITY patterns) — closes gaps where lead-user reference
  had inline `<!-- IMMUTABILE -->` markers without matching registry regex.

### Fixed

- **Sync feedback bug** (Phase 2.5 → 2.6): the sync script previously read its
  own output as "framework boilerplate" on subsequent runs, ballooning RULES.md
  by ~70% per run. Operational tail relocated to a sync-safe path
  (`templates/persona-interview/framework-rules-tail.md`); sync is now byte-stable.

## 2026.05.02

This release consolidates corporate-readiness work for the email channel,
introduces the autonomous user-model pipeline, hardens Codex sandbox defaults
and Telegram authorization, and ships guided setup + shell completion.

### Upgrade notes for existing instances

The framework auto-migrates where it can; the items below need an operator
action after `git pull && ./install.sh`.

1. **Re-run installer** — new shims: `jc-email`, `jc-completion`,
   `jc-user-model`, `jc-codex-auth`, `jc-update`, `jc-chats`, `jc-transcripts`.
   `./install.sh` installs them; `jc doctor` flags missing ones.
2. **Codex sandbox now defaults to `read-only`.** Instances that relied on the
   previous default (no `CODEX_SANDBOX` env, effectively unrestricted) will
   start refusing writes. To restore prior behavior, set in `ops/gateway.yaml`:
   ```yaml
   brains:
     codex:
       sandbox: workspace-write   # or yolo: true for full access
   ```
   `gateway.yaml` is now schema-validated and rejects unknown sandbox values
   and `yolo=true` paired with a non-yolo sandbox.
3. **Telegram default-deny.** New chats and groups are recorded as `pending`
   and dropped until an operator approves them. `TELEGRAM_CHAT_ID` and
   `channels.telegram.chat_ids` allowlists still bypass the DB. Approve with
   `jc chats approve <chat_id>`. Existing rows keep their prior `auth_status`.
4. **Memory DB schema reset.** `state: active` is now an accepted frontmatter
   value. `lib/memory/db.connect()` auto-drops the derived `entries`,
   `entries_fts`, and `backlinks` tables when the old CHECK constraint is
   detected. Run `jc memory rebuild` after the first `jc-memory` invocation
   to repopulate the index from markdown.
5. **L1 RULES.md needs a manual sync.** Templates only apply at instance
   creation. Pull in the new canonical sections (`Conversation transcripts`,
   `HOT.md structure`):
   ```sh
   scripts/sync_l1_rules.py --instance-dir <instance> --dry-run
   scripts/sync_l1_rules.py --instance-dir <instance>
   ```
   Idempotent; existing sections are left alone.
6. **Heartbeat now inherits MCP config.** Tasks no longer run with
   `--mcp-config '{}' --strict-mcp-config`. If a task relied on MCPs being
   off, gate it explicitly. Sessions are also captured via pre/post snapshot
   and persisted under `heartbeat/state/<task>.session` for `--resume`.
7. **Email channel and autonomous user model are opt-in.** Both ship disabled
   in templates (`channels.email.enabled: false`, `ops/user_model.yaml`
   `enabled: false`). Existing instances are unaffected unless enabled.

### Added

- **Codex main-brain hardening (Phases 1–8).** Per
  `docs/specs/codex-main-brain-hardening.md`. Closes the audit bug where
  `default_brain: codex:gpt-5.4-mini` silently fell back to claude.
  - Config correctness: `<brain>:<model>` parsed before `SUPPORTED_BRAINS`
    check; bad brain rejected, valid model preserved.
  - Context parity: non-Claude brains receive a preamble semantically
    equivalent to Claude's `CLAUDE.md` import (instance role contract,
    expanded L1 memory including auto-generated `CHATS.md`, L2 retrieval
    guidance, framework command hints, token-efficiency rules).
  - Session capture safety: `CodexBrain.capture_session_id` uses a pre/post
    snapshot diff of `~/.codex/sessions/` instead of a timestamp-only scan,
    so concurrent Codex processes can't poison the resume id.
  - `codex_api` transcript priming: resumed sessions now receive a priming
    block + system instructions (was a no-op, losing continuity per call).
    `resume_session` remains a no-op for `codex_api` by design.
  - Brain capability matrix replaces the vision hardcode; `--image`
    routing keyed off declared capabilities, not brain name.
  - `jc doctor` Codex section: short-name aliases, `auth.json` presence,
    instance `.codex/` vs `CODEX_HOME` mismatch warning, sandbox warning
    when `default_brain: codex` pairs with a write-capable sandbox.
- **Email channel (IMAP/SMTP) — corporate-ready.** Bidirectional channel with
  sender allowlist, prompt-injection sanitization, atomic YAML writes,
  pending-inbound + draft approval flow, lifecycle events, and a runbook.
  - `jc email` first-class CLI: `senders`, `pending`, `drafts`, `doctor`,
    `test-imap`, `test-smtp`.
  - `jc chats approve|deny --email <addr>` for cross-channel sender ops.
  - Heartbeat-driven IMAP poller (`heartbeat/fetch/email-poll.sh`).
  - Gateway dispatcher routes allowed mail to the queue, parks unknown
    senders for operator review, persists drafts before send.
  - Doctor checks: credential presence, pending counts, last-UID liveness.
  - Spec: `docs/specs/email-channel.md`. Runbook:
    `docs/runbooks/email-operations.md`.
- **Autonomous user model (`jc-user-model`).** Detects recurring topics, comm
  preferences, priority shifts, new entities, and rule drift across sessions
  and proposes memory/L1/USER.md updates.
  - Pipeline: corpus → 5 detectors → proposer (LLM) → store (JSONL, dedup,
    cooldown) → applier (atomic + `.history/` backup) → notify.
  - CLI: `run-now`, `review`, `apply`, `reject`, `install`, `uninstall`,
    `status`. Cron-installable.
  - Config: `ops/user_model.yaml` (template ships `enabled: false`).
  - Spec: `docs/specs/autonomous-user-model.md`.
- **Guided setup + shell completion.**
  - `jc setup` is now a guided walkthrough (brain choice, Telegram bootstrap,
    secret prompts) wrapping the previous one-shot flow.
  - `jc completion bash|zsh|fish` prints completion scripts.
  - New `docs/jc-command-catalog.md` enumerates every routed subcommand.
- **Codex direct-API auth (`jc-codex-auth`).** Extracts ChatGPT-subscription
  credentials so codex calls can hit the OpenAI API directly. Spec:
  `docs/specs/codex-auth-extractor.md`.
- **Telegram chat-import script.** `scripts/` helper that imports a Telegram
  desktop chat export into the transcripts store.
- **Sender approval prompt.** Unauthorized senders trigger an operator prompt
  in Telegram before the message is dropped or accepted; config-only allowlist
  changes are honored without restart. Specs:
  `docs/specs/gateway-sender-approval.md`,
  `docs/specs/sender-approval-config-only.md`.
- **Memory `noindex: true` flag.** Files with this frontmatter are skipped by
  the indexer (counted as silent skip, not error).
- **L1 sync helper (`scripts/sync_l1_rules.py`).** Appends missing canonical
  H2 sections to an existing instance's `memory/L1/RULES.md`.
- **KB re-verification.** All 20 KB entries re-verified against current code;
  see `docs/kb/LOG.md`.

### Changed

- **Codex sandbox default → `read-only`** when `brains.codex.sandbox` is
  unset and `yolo` is not true. Validator rejects unknown sandbox values,
  non-bool `yolo`, and `yolo=true` with a non-yolo sandbox.
- **Telegram authorization is fail-closed.** Unknown chats and DB lookup
  failures default to deny. Auth check runs before `_record_chat` so the
  operator still sees inbound chats and can approve them.
- **Memory parser** validates the `state` frontmatter value; `rebuild`
  skip-and-continues on a single bad file instead of aborting. Schema accepts
  `active` alongside `draft|reviewed|verified|stale|archived`.
- **Heartbeat runner** drops the MCP override and captures session ids via
  pre/post snapshot diff (no more mtime race), persisting them per task for
  `--resume` continuity.
- **`jc-chats` and `jc-email`** share the email-policy module
  (`lib/gateway/channels/email_policy.py`); operator guidance now points to
  `jc email senders` for new work.
- **Workers** set `JC_IN_WORKER=1` to break recursion when a worker spawns
  another worker.

### Fixed

- Memory: accept `active` as a frontmatter state and surface its `·` marker in
  the index.
- Email: prompt-injection sanitization on inbound bodies; atomic YAML writes
  for state files.
- Triage: drop stale `max_output_tokens` kwarg from the `codex_api` triage
  caller.
- Telegram: approval prompts for new senders no longer race against
  `_record_chat`.
- Install: `jc-user-model`, `jc-chats`, `jc-update` were missing from the shim
  list and router — added.

### Security

- Telegram default-deny closes a fail-open auth-bypass window where new chats
  were auto-authorized at first inbound.
- Codex sandbox: read-only by default, explicit opt-in for write access.

### Schema

- Gateway DB stays at `schema_version = 4` (`auth_status` DEFAULT changed
  from `'allowed'` to `'pending'`; existing rows preserved).
- Memory derived index: CHECK constraint widened to include `'active'`. Old
  tables dropped on first connect when the constraint is stale; rebuild
  required.

## 2026.04.28

Earlier work shipped under unified releases. See `git log v2026.04.28..` for
the diff that produced 2026.05.02. The major themes since 0.3.0:

- Unified gateway daemon (telegram, slack, discord, voice, jc-events, cron).
- Multi-brain Python wrappers (claude, codex, gemini, opencode, aider) with
  `[brain]` and `/brain` overrides.
- Triage layer with ollama / openrouter / claude-channel backends and sticky
  brain.
- `jc migrate-to-0.3` migrator, structured JSON logs, backpressure, log
  rotation.
- `docs/GATEWAY.md`, migration guide, ADR, brain capability matrix.
- Config schema validator.
