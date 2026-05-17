# Changelog

All notable changes to JuliusCaesar are documented here. Versions follow CalVer
(`YYYY.MM.DD`). Newest first.

## Unreleased

## 2026.05.17.02

Release for video ingestion + WhatsApp shim wiring.

- Video ingestion uses split-and-fuse: DashScope omni (`asr.py`)
  transcribes the audio track; `lib/voice/vlm.py` describes frames via
  `qwen3.6-plus` (DashScope intl compatible-mode endpoint).
  `lib/voice/video.py` orchestrates the ffmpeg audio split, VLM call,
  cleanup, and fuses the two outputs into a single brain-ready event
  text.
- `lib/gateway/channels/telegram.py` recognises Telegram `video`
  payloads, downloads under `state/voice/inbound/`, ingests when
  `voice.video.enabled` is true in `ops/gateway.yaml`, and forwards the
  fused text plus paths in event meta. Files larger than 50 MB receive a
  polite rejection notice and are skipped.
- `voice` added to gateway config `allowed_top` so `voice.video.*`
  passes validation.
- `bin/jc-whatsapp` added to `install.sh` BINARIES — shim missing from
  the 2026.05.17.01 release. Re-run `./install.sh` on any host upgrading
  from `2026.05.17.01` to land the shim.
- Spec at `docs/specs/video-ingestion.md`.

## 2026.05.17.01

Release for the WhatsApp channel.

- New WhatsApp channel ships end-to-end: TypeScript sidecar
  (`lib/gateway/channels/whatsapp_sidecar/`) talks to WhatsApp Web via
  Baileys; the Python channel (`lib/gateway/channels/whatsapp.py`) hosts
  the inbound DM path, policy, protocol, sidecar supervisor, and state.
- `bin/jc-whatsapp` operator CLI drives pairing / approval / health.
- Approval flow integrates with the unified approvals table; blocked
  senders are rejected before policy evaluation; watchdog health check
  exposes `auth_valid` for the sidecar.
- Phase 5 media download pipes inbound WhatsApp media through the
  gateway's `voice/inbound/` directory for parity with Telegram.
- Spec at `docs/specs/whatsapp-channel.md`; KB entry at
  `docs/kb/subsystem/channel-whatsapp.md`.

## 2026.05.15.02

Release for accountabilities and deep-research operator tooling.

- New `deep-research` skill drives Gemini Advanced through a per-host
  Chromium profile. Adds `bin/jc-research` (login / run / start / status /
  result / cancel / list / profile), the `lib/skills/gemini_deep_research/`
  package, and a `deep-research/SKILL.md` template instance skill.
- `jc-events` channel now renders `research.completed` events into a
  persona-voice synthesis prompt (and a separate failure branch) so deep
  research jobs surface back through the standard chat path.
- `install.sh` adds `browser-use` + `playwright` to the venv and runs
  `playwright install chromium` idempotently after the venv install.
- `jc skills` lists the new skill (`required_env=()` — login is per-host,
  not per-instance) and exposes a tester that checks profile freshness +
  Playwright importability.
- Operator opt-out: `JC_RESEARCH_DISABLED=1` in an instance `.env` makes
  every CLI entry exit 17 immediately.
- Added the opt-in accountabilities governance layer: config schema,
  L1 manifest + L2 detail templates, `jc memory scaffold accountabilities`,
  audit-log writer, `jc-doctor` health checks, operator docs, and KB coverage.
- Accountabilities context is injected only when `accountabilities.enabled` is
  true; disabled instances do not load the manifest even when the file exists.
- Manifest enactment authority now surfaces live config to the agent, including
  the concrete `telegram_primary_chat_id` for `telegram-primary`, and audit
  health checks parse escaped Markdown table pipes correctly.

## 2026.05.15.01

Release for pi.dev as a first-class gateway brain.

- Added the `pi` brain wrapper, shell adapter, model aliases, config support,
  capability matrix entry, and focused gateway tests.
- pi gateway runs now inject relevant provider API keys from instance `.env`,
  including `GEMINI_API_KEY` for Google/Gemini models, while keeping API keys
  off the command line.
- The pi adapter disables context-file, extension, skill, prompt-template, and
  theme discovery by default so gateway prompts remain deterministic.
- Added pi-friendly aliases for Claude, OpenAI, and Google/Gemini models,
  including `pi-google`, `pi-gemini25`, and `pi-gemini20`.
- `jc doctor` now reports the optional `pi` CLI dependency.

## 2026.05.12.01

Commercial v1 for offline self-improvement.

- Added `jc commitments`, a durable YAML deferred-action engine with
  timezone-aware due times, retries, done/failed archives, daily/weekly repeats,
  `telegram-send` and `jc-event` actions, and a `commitments_tick` heartbeat
  builtin.
- Added opt-in re-engagement: `ops/reengage.yaml`, template-backed touch
  messages under `memory/L2/templates/re-engagement/`, hard touch caps,
  allowed slots/quiet hours, and immediate gateway-side cancellation when a
  tracked chat replies.
- Added `jc dream`, the offline reflection cycle. Dream reads transcripts,
  heartbeat sent records, memory frontmatter, and closed commitments; runs
  self-model signal and memory hygiene checks; emits playbooks, learnings,
  backlink stubs, and verification commitments; writes markdown reports under
  `state/dreams/`; and exposes `tick`, `dry-run`, `run`, `list`, `show`,
  `pending`, `approve`, and `reject`.
- Added risk gates for dream artifacts: LOW/MEDIUM auto-apply with retained
  rollback metadata, SENSITIVE stage-only diffs, and frozen-target rejection.
- Fresh instances now scaffold commitments state, dream review state,
  `memory/L2/playbooks/`, disabled `commitments_tick`, `reengage_tick`, and
  `dream_tick` heartbeat tasks, and RULES §24/§25/§26 for re-engagement,
  sweeps, and autonomous follow-through.
- Added the `2026.05.12.01` release update hook so older instances receive the
  commitments/re-engage scaffold automatically through `jc update`.
- KB entries now document commitments/re-engagement and the dream pipeline.

## 2026.05.09.01

Release bundle for the pending gateway and operator-config PRs.

- `jc-upgrade` now rewrites `ops/gateway.yaml` through a non-destructive merge:
  prompted framework-owned fields are updated, while operator-owned blocks such
  as `reply_footer`, `reliability`, `brains`, email config, blocked chat IDs,
  explicit disabled channels, nested triage config, and `default_model` survive.
- Unsafe triage verdicts can now route through `triage_unsafe_fallback_brain`
  instead of silently dropping the message. The new OpenRouter brain is limited
  to that unsafe-fallback path and is rejected for normal overrides/defaults.
- Persona tone anchoring adds `memory/L1/STYLE.md`, loads it after IDENTITY,
  injects the `[Voice: ...]` anchor every turn, and lets instances opt into
  `pin_to_default_brain` when triage model swaps would damage tone.
- Caveman mode is now off by default. `STYLE.md` ships with
  `caveman: disabled`; only an explicit `caveman: enabled` injects caveman
  compression guidance.
- Integrated safety fix: `pin_to_default_brain` suppresses normal model swaps
  and vision auto-routing, but it does not suppress the unsafe fallback brain.

## 2026.05.08.01

Patch release for email sender-policy ergonomics.

- Unlisted email senders now default to runtime `external` behavior instead of
  creating pending inbound approval items. The assistant can process the email,
  but outbound replies still become approval drafts unless the sender is
  explicitly trusted.
- Sender policy names remain unchanged: `trusted`, `external`, and
  `blocklist`. Missing senders are not auto-written into `senders.external`;
  durable sender classification still happens only when the operator or agent
  explicitly updates policy.
- External/default-external inbound messages now send a best-effort operator
  notification without creating a pending approval gate, so the main chat can
  promote the sender to `trusted` or move it to `blocklist`.
- Empty/malformed sender identities and invalid adapter status values are
  rejected before enqueue instead of silently becoming external.
- Outbound email replies re-resolve the current sender policy at send time, so
  promotion to `trusted` or `blocklist` changes the current reply behavior.
- Existing pending inbound messages from earlier releases remain supported and
  drainable through `jc email pending ...` and `jc email senders ...`.
- `parse_brain_output` now treats a silent sentinel (`SILENT`, `SILENCE`,
  `[SILENT]`, `[NO-REPLY]`, `[NO_REPLY]`, `[SKIP]`, `NO_REPLY`, `NO-REPLY`)
  appearing inside the envelope `message` field as an explicit no-op when
  `push_message_sent` is false. Previously only raw or trailing-line sentinels
  were suppressed, so a brain that wrapped the sentinel in the canonical JSON
  envelope (`{"push_message_sent": false, "message": "SILENT"}`) caused the
  literal token to be relayed to users. Audit messages on `push_message_sent=true`
  envelopes are unchanged.
- The embedded-envelope recovery path applies the same suppression so brains
  that emit prose plus an envelope cannot leak sentinels either.

## 2026.05.07.01

Hotfix for instance-owned voice credentials and Telegram voice replies.

- Voice ASR/TTS helpers now read `DASHSCOPE_API_KEY` through the instance
  `.env` loader, so clean-env gateway and cron launches keep per-instance
  credentials isolated instead of depending on exported shell variables.
- `jc-voice` now passes the resolved instance directory into transcription and
  synthesis helpers, preserving the same `.env` behavior for CLI smoke tests.
- Telegram voice events now bypass text-content triage and route through the
  configured `voice` class. This prevents short voice checks from being
  misclassified as `smalltalk` and sent to a lightweight model.
- Brain prompts for transcribed voice events now require same-language,
  spoken-natural replies before the gateway renders optional TTS output.
- Instance env helpers ignore reserved runtime-control names such as `PATH`,
  `RUNTIME_MODE`, `JC_*`, and `WORKER_*` when reading or merging `.env` values.

## 2026.05.06.01

Skill management for pre-shipped agent tools.

- Added `jc skills`, an interactive and scriptable command for inspecting
  instance skill status, syncing pre-shipped skills into older instances,
  writing managed provider credentials to `.env`, and testing provider
  configuration.
- Managed skills currently cover Brave Search, Tavily, Firecrawl, and Browser
  Use. Status is active when the skill file exists and the required credential
  is present.
- `jc skills test` validates credentials against low-impact provider account
  or search endpoints and records the last redacted result under
  `state/skills/status.json`.
- Installer, router, shell completions, doctor diagnostics, command catalog,
  and KB contracts now include the skills command.

## 2026.05.05.2

Hotfix for operator safety, command UX, and duplicate-message suppression.

- `jc persona interview` longtext prompts now finish with an `EOF` line instead
  of a blank line, so pasted multi-paragraph content keeps internal paragraph
  breaks instead of bleeding into the next prompt.
- Multi-prompt persona slots now show a composed-body preview and require
  apply / re-do / abort before splicing into L1 memory files.
- Shell completion now includes nested `jc email pending|senders|drafts`
  subcommands.
- New instance instructions now tell agents to create or install instance-owned
  skills at `$JC_INSTANCE_DIR/skills/<skillname>/SKILL.md`.
- Heartbeat runner now suppresses Telegram delivery when the brain emits a
  trailing `SILENT` line, mirroring the gateway runtime fix from `788447f`.
  Previously the heartbeat path bypassed gateway suppression and shipped the
  full narration + literal `SILENT` to Telegram as a duplicate message.
- **Structured brain-output contract** replaces the ad-hoc `SILENT` sentinel.
  Brains now emit a single JSON object on stdout:
  `{"push_message_sent": <bool>, "message": <string>}`. The framework reads
  the flag to decide whether to deliver `message` (when false) or treat it as
  audit log of an already-pushed PushNotification (when true). One channel
  per source, no more case-by-case suppression heuristics. Parser falls back
  to delivering raw stdout on parse failure so brains that haven't migrated
  yet still surface their output to the user.
  - New module: `lib/gateway/brain_output.py`.
  - `lib/gateway/runtime.py` and `lib/heartbeat/runner.py` route through the
    parser; the legacy `SILENT`-detection branches are removed.
  - System-prompt updated in `lib/heartbeat/adapters/claude.sh` and
    `CODEX_API_INSTRUCTIONS` to enforce the contract.
  - `lib/heartbeat/adapters/codex.sh` now prepends the contract to the prompt
    body via process substitution (codex CLI has no `--append-system-prompt`
    flag), so codex CLI is first-class with the JSON contract too.
  - Remaining adapters (aider, gemini, minimax, opencode) gracefully degrade
    via the parser's raw-fallback until their prompts are migrated.

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
the diff that produced 2026.05.02. The major themes in that CalVer release:

- Unified gateway daemon (telegram, slack, discord, voice, jc-events, cron).
- Multi-brain Python wrappers (claude, codex, gemini, opencode, aider) with
  `[brain]` and `/brain` overrides.
- Triage layer with ollama / openrouter / claude-channel backends and sticky
  brain.
- CalVer release update hook, structured JSON logs, backpressure, log rotation.
- `docs/GATEWAY.md`, migration guide, ADR, brain capability matrix.
- Config schema validator.
