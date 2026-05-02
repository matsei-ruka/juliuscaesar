# Changelog

All notable changes to JuliusCaesar are documented here. Versions follow CalVer
(`YYYY.MM.DD`). Newest first.

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
