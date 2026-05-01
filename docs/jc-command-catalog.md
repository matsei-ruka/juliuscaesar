# JC Command Catalog

Status: Inventory draft
Date: 2026-05-01

## Purpose

This is the current command surface for JuliusCaesar. It is intentionally a
catalog before a redesign: the CLI is broad, partly duplicated, and not fully
consistent. Use this file as the map for cleanup.

Sources checked:

- `bin/jc`
- every `bin/jc-*` binary installed by `install.sh`
- argparse builders in `lib/company/cli.py`, `lib/user_model/cli.py`,
  `lib/codex_auth/cli.py`, and `lib/watchdog/cli.py`
- `install.sh` `BINARIES=(...)`

## Router

`jc` is a Bash router. It accepts optional global `--instance-dir <path>` or
`--instance-dir=<path>`, then dispatches `jc <name>` to `jc-<name>` on `PATH`.

Router version: `2026.04.28`.

Routed public subcommands:

| `jc` command | Binary | Area | Notes |
|---|---|---|---|
| `jc memory` | `jc-memory` | memory | Markdown memory entries and FTS index |
| `jc heartbeat` | `jc-heartbeat` | scheduled tasks | Runs one configured heartbeat task |
| `jc voice` | `jc-voice` | voice | DashScope TTS, ASR, voice enrollment |
| `jc watchdog` | `jc-watchdog` | supervision | Legacy plus v2 watchdog surface |
| `jc workers` | `jc-workers` | background agents | Spawn/list/show/tail/cancel worker jobs |
| `jc chats` | `jc-chats` | channel ops | Telegram chat directory and compatibility email sender aliases |
| `jc email` | `jc-email` | channel ops | Email doctor, sender policy, pending inbox, draft approval |
| `jc transcripts` | `jc-transcripts` | gateway history | Per-conversation transcript read/search/tail |
| `jc gateway` | `jc-gateway` | gateway runtime | Queue, daemon, logs, config, triage metrics |
| `jc company` | `jc-company` | fleet ops | Company registration, outbox replay, alerts, approvals |
| `jc init` | `jc-init` | setup | Scaffold a new instance |
| `jc setup` | `jc-setup` | setup | Guided first-run configurator |
| `jc update` | `jc-update` | framework lifecycle | Pull latest released framework |
| `jc upgrade` | `jc-upgrade` | instance lifecycle | Reconfigure an existing instance |
| `jc doctor` | `jc-doctor` | diagnostics | Instance and environment health checks |
| `jc user-model` | `jc-user-model` | user model | Autonomous user model proposal loop |
| `jc codex-auth` | `jc-codex-auth` | auth | Local Codex OAuth token inspection/refresh |
| `jc self-model` | `jc-self-model` | persona | Autonomous self-observation loop (proposes JOURNAL/RULES/IDENTITY edits) |
| `jc migrate-to-0.3` | `jc-migrate-to-0.3` | migration | Bootstrap older instances for unified gateway |
| `jc completion` | `jc-completion` | shell UX | Print bash/zsh completion scripts |

Installed but not routed by `jc`:

| Binary | Purpose | Notes |
|---|---|---|
| `test-gateway-smoke` | Gateway smoke test helper | Not in `install.sh` `BINARIES`; not a public `jc` command |

## Global Flags

Common pattern:

```bash
jc --instance-dir /path/to/instance <subcommand> ...
```

Reality:

- The router forwards `--instance-dir` to routed commands.
- Most instance-aware binaries implement `--instance-dir`.
- `jc-codex-auth` does not use an instance dir.
- Some Bash binaries parse help and flags differently from Python argparse
  binaries.

## Command Details

### `jc memory`

Binary: `bin/jc-memory`

Purpose: manage instance memory markdown files and the SQLite FTS index.

Subcommands:

| Subcommand | Arguments/options | Purpose |
|---|---|---|
| `new` | `slug`, `--title`, `--layer L1|L2`, `--type`, `--state draft|reviewed|verified|stale|archived`, `--tag`, `--link`, `--body` | Create a memory entry |
| `write` | `slug`, `--body-file` or stdin | Replace an entry body |
| `read` | `slug` | Print one entry |
| `search` | `query`, `--limit` | FTS search |
| `link` | `slug`, `--to` | Add a wikilink |
| `lint` | none | Broken links, orphans, staleness |
| `log` | optional `n` | Tail `LOG.md` |
| `rebuild` | none | Re-scan markdown and rebuild DB/index |
| `consolidate` | none | Placeholder auto-dream command |

### `jc heartbeat`

Binary: `bin/jc-heartbeat`

Purpose: run configured scheduled tasks.

Subcommands:

| Subcommand | Arguments/options | Purpose |
|---|---|---|
| `run` | `task`, `--dry-run` | Execute one heartbeat task |

### `jc voice`

Binary: `bin/jc-voice`

Purpose: DashScope voice synthesis, transcription, and voice enrollment.

Subcommands:

| Subcommand | Arguments/options | Purpose |
|---|---|---|
| `speak` | `text`, `--out` | Synthesize text to OGG/Opus |
| `transcribe` | `audio`, `--out` | Transcribe an audio file |
| `enroll` | `audio`, `--name`, `--target-model` | Enroll a voice sample and write `voice/references/voice.json` |
| `list-voices` | none | List enrolled DashScope voices |

### `jc watchdog`

Binary: `bin/jc-watchdog`

Purpose: supervise the live gateway or legacy Claude session.

Subcommands:

| Subcommand | Arguments/options | Purpose |
|---|---|---|
| `tick` | none | Run one supervisor tick; default when no subcommand is provided |
| `install` | none | Install `@reboot` and `*/2` cron entries |
| `uninstall` | none | Remove this instance's cron entries |
| `status` | none | Show legacy state, cron entries, and recent log |
| `status-v2` | `--json` | Show v2 child status table |
| `tail` | `child`, `-n/--lines`, `-f/--follow` | Tail a v2 child log |
| `reset` | `child` or `all` | Clear alert mode and restart counters |
| `reload` | none | Re-read `ops/watchdog.yaml` and run one tick |
| `migrate` | `--force` | Generate `ops/watchdog.yaml` from legacy config |
| `test-notify` | none | Send a Telegram notification test |

Notes:

- `status-v2`, `tail`, `reset`, `reload`, and `migrate` delegate to
  `python -m watchdog.cli`.
- `tick` chooses v2 supervisor when `ops/watchdog.yaml` exists, otherwise
  legacy `lib/watchdog/watchdog.sh`.

### `jc workers`

Binary: `bin/jc-workers`

Purpose: spawn and manage detached background agent jobs.

Subcommands:

| Subcommand | Arguments/options | Purpose |
|---|---|---|
| `spawn` | `--topic`, `--brain`, `--model`, `--notify`, `--timeout`, `--spawned-by`, `--telegram-msg-id`, `--prompt`, `--name`, `--tag`, `--fresh`, `--read-only`, `--yolo` | Start a background worker |
| `_run` | `worker_id` | Internal worker runner; hidden from help |
| `list` | `--status`, `--name`, `--tag`, `--limit` | List worker rows |
| `show` | `worker_id` or `--name` | Show one worker |
| `tail` | `worker_id` | Follow worker log |
| `cancel` | `worker_id` | Cancel a running worker |
| `gc` | `--days`, `--prune-files` | Delete old completed workers |
| `reconcile` | none | Mark stale running rows failed when pid is dead |
| `history` | `--name` | Show runs for a named worker |

### `jc chats`

Binary: `bin/jc-chats`

Purpose: inspect and manage channel chat identity records. It also carries
compatibility email sender approval aliases.

Subcommands:

| Subcommand | Arguments/options | Purpose |
|---|---|---|
| `list` | `--channel`, `--limit`, `--auth-status allowed|pending|denied`, `--json` | List chats by `last_seen` |
| `approve` | optional `chat_id`, `--channel`, `--email` | Approve a chat id or trust an email sender |
| `deny` | optional `chat_id`, `--channel`, `--email` | Deny a chat id or block an email sender |
| `show` | `chat_id`, `--channel`, `--json` | Show one chat row |
| `migrate-to-config` | `--channel`, `--dry-run` | Lift DB auth rows into config |
| `prune` | `--older-than`, `--channel`, `--yes` | Delete old chat rows |

Cleanup signal:

- Email sender policy now has `jc email senders`; the `jc chats --email`
  aliases should probably be documented as compatibility-only or retired later.

### `jc email`

Binary: `bin/jc-email`

Purpose: operate the email channel: diagnostics, sender policy, pending inbound
mail, and outbound draft approvals.

Subcommands:

| Subcommand | Arguments/options | Purpose |
|---|---|---|
| `doctor` | `--json` | Summarize email config, credentials, state, events |
| `test-imap` | none | Connect/login/select configured IMAP mailbox |
| `test-smtp` | none | Connect/login to configured SMTP server |
| `pending list` | `--sender`, `--json` | List pending inbound mail |
| `pending show` | `uid` | Show one pending message |
| `pending approve` | `sender` | Drain pending sender into queue as trusted |
| `pending deny` | `sender` | Drop pending sender messages |
| `senders list` | `--json` | List trusted, external, and blocked senders |
| `senders trust` | `sender` | Trust sender and drain pending mail |
| `senders external` | `sender` | Mark external and drain pending mail |
| `senders block` | `sender` | Block sender and drop pending mail |
| `drafts list` | `--sender`, `--all`, `--json` | List drafts |
| `drafts show` | `draft_id` | Show one draft |
| `drafts edit` | `draft_id`, optional `text` or stdin | Replace draft body |
| `drafts approve` | `draft_id` | Send one pending draft |
| `drafts reject` | `draft_id` | Reject one pending draft |

### `jc transcripts`

Binary: `bin/jc-transcripts`

Purpose: inspect per-conversation transcript files.

Subcommands:

| Subcommand | Arguments/options | Purpose |
|---|---|---|
| `read` | `conversation_id` | Print full transcript |
| `tail` | `conversation_id`, `--lines`, `--since` | Show recent transcript events |
| `search` | `query`, `--since`, `--role user|assistant`, `--limit` | Search transcripts |
| `get` | `message_id` | Find one event by message id |
| `list` | none | List known conversation ids |

### `jc gateway`

Binary: `bin/jc-gateway`

Purpose: operate the durable gateway queue and daemon.

Subcommands:

| Subcommand | Arguments/options | Purpose |
|---|---|---|
| `init` | none | Create or migrate queue DB |
| `status` | none | Show queue path and counts |
| `run` | `--interval-seconds` | Run foreground dispatcher |
| `start` | `--interval-seconds` | Start daemon |
| `stop` | `--timeout-seconds`, `--kill` | Stop daemon |
| `restart` | `--interval-seconds`, `--timeout-seconds`, `--kill` | Restart daemon |
| `tail` | `-n/--lines`, `-f/--follow` | Print/follow gateway log |
| `logs` | `-n/--lines`, `-f/--follow`, `--since`, `--class`, `--brain`, `--source`, `--limit` | Filter JSON gateway log |
| `enqueue` | optional `content`, `--source`, `--source-message-id`, `--user-id`, `--conversation-id`, `--meta` | Enqueue an event |
| `claim` | `--worker-id`, `--lease-seconds` | Claim one ready event |
| `complete` | `event_id`, `--response` | Mark event done |
| `fail` | `event_id`, `--error`, `--max-retries` | Mark failed or schedule retry |
| `list` | `--limit`, `--json` | List events |
| `events` | `--limit`, `--json` | Alias-like event list |
| `retry` | `event_id` | Requeue event now |
| `config` | `--json` | Show resolved config |
| `validate-config` | none | Validate `ops/gateway.yaml` |
| `work-once` | `--worker-id`, `--lease-seconds`, `--mode echo` | Local smoke processing |
| `metrics` | `--hours`, `--json` | Print recent triage metrics |
| `reload` | none | Send SIGHUP to daemon |

Cleanup signal:

- `list` and `events` overlap.
- Queue maintenance commands and daemon operations share one large namespace.

### `jc company`

Binary: `bin/jc-company`

Purpose: connect an instance to the Company dashboard and manage outbox events,
alerts, and approvals.

Subcommands:

| Subcommand | Arguments/options | Purpose |
|---|---|---|
| `register` | `--endpoint`, `--token`, `--name` | Enroll instance |
| `status` | `--ping` | Show local Company state and optionally heartbeat |
| `alert` | `title`, `--severity`, `--body`, `--link` | Raise an alert |
| `approval` | `title`, `--type`, `--body`, `--payload`, `--media`, `--expires-in`, `--wait` | Raise and optionally wait for approval |
| `replay` | `--since` | Replay buffered outbox events |

### `jc init`

Binary: `bin/jc-init`

Purpose: scaffold a new instance from `templates/init-instance`.

Usage:

```bash
jc init [path]
```

Behavior:

- creates target directory;
- refuses existing `.jc`;
- refuses non-empty target except `.git`, `.gitignore`, `README.md`, `LICENSE`;
- copies template tree;
- writes `.jc`;
- creates `.gitignore` and mode-600 `.env` when missing.

Help status: fixed; `jc init --help` prints usage.

### `jc setup`

Binary: `bin/jc-setup`

Purpose: guided first-run configurator.

Usage:

```bash
jc setup [path] [--defaults] [--force] [--start] [--no-start] [--install-watchdog] [--no-watchdog] [--no-wait]
```

Options:

| Option | Purpose |
|---|---|
| `--defaults` | Non-interactive defaults; useful in tests/automation |
| `--force` | Overwrite generated bootstrap memory/watchdog files |
| `--start` | Start gateway daemon after setup |
| `--no-start` | Do not start a live session |
| `--install-watchdog` | Install watchdog cron entries after setup |
| `--no-watchdog` | Do not install watchdog cron entries |
| `--no-wait` | Configure without waiting for first Telegram message |
| `--channel telegram` | First communication channel; currently Telegram |

Current flow:

1. Detect coding tools (`claude`, `codex`, `gemini`, `opencode`, `aider`) and
   only offer tools that appear available and logged in.
2. Quit if no usable coding tool exists.
3. Select the default gateway brain.
4. Configure the first communication channel, currently Telegram.
5. Wait for the first Telegram message, then write that chat id to `.env` and
   `channels.telegram.chat_ids`.
6. Send the first onboarding question and create bootstrap L1 memory files that
   tell the selected brain to drive agent creation one question at a time.

### `jc update`

Binary: `bin/jc-update`

Purpose: check GitHub releases and optionally update framework code.

Usage:

```bash
jc update [--check-only]
```

Behavior:

- reads local version from `pyproject.toml`;
- fetches latest release from GitHub;
- prompts before `git fetch origin` plus `git reset --hard origin/main`;
- attempts `jc-watchdog reload`.

Help status: fixed. Update now uses `git merge --ff-only origin/main` instead
of `git reset --hard`.

### `jc upgrade`

Binary: `bin/jc-upgrade`

Purpose: interactive reconfiguration of an existing instance.

Usage:

```bash
jc upgrade [--instance-dir path] [--defaults] [--restart]
```

Options:

| Option | Purpose |
|---|---|
| `--defaults` | Non-interactive; keep existing values as-is |
| `--restart` | Restart gateway daemon at the end; otherwise reload |

Prompts/configures:

- Telegram token and chat id;
- Slack app and bot tokens;
- Discord bot token;
- voice/DashScope;
- default brain;
- triage backend and model settings;
- triage confidence threshold;
- fallback brain;
- sticky brain timeout;
- per-class routing;
- watchdog runtime mode.

Help status: fixed. Remaining cleanup signal: it rewrites `ops/gateway.yaml`
via heredoc and backs up the previous file, rather than using the shared config
writer.

### `jc doctor`

Binary: `bin/jc-doctor`

Purpose: diagnose environment, instance structure, credentials, gateway config,
memory, voice, watchdog, workers, and email channel state.

Usage:

```bash
jc doctor [--instance-dir path] [--fix]
```

Options:

| Option | Purpose |
|---|---|
| `--fix` | Apply conservative repairs: permissions, missing memory index, gateway queue/config, stale pidfiles, `state/` |

Checks include:

- required binaries: `python3`, `bash`, `curl`, `screen`, `ffmpeg`, `git`;
- Claude CLI;
- installed `jc-*` binaries;
- optional adapter CLIs: `gemini`, `opencode`, `codex`;
- Python `websocket-client`;
- instance marker and expected directories;
- gateway config validation;
- memory index;
- `.env` mode and selected credentials;
- voice enrollment;
- gateway daemon/watchdog state;
- worker state;
- email channel credentials and local metrics.

Help status: fixed.

### `jc user-model`

Binary: `bin/jc-user-model`

Purpose: autonomous user-model corpus/detector/proposal loop.

Subcommands:

| Subcommand | Arguments/options | Purpose |
|---|---|---|
| `run-now` | none | Execute one cycle |
| `status` | none | Show pending proposals and last run |
| `review` | `--id`, `--limit` | List/filter pending proposals |
| `apply` | `proposal_id` | Apply a proposal |
| `reject` | `proposal_id`, `--reason` | Reject a proposal |
| `install` | `--cadence` | Install cron task |
| `uninstall` | none | Remove cron task |

### `jc codex-auth`

Binary: `bin/jc-codex-auth`

Purpose: inspect and refresh the local Codex CLI OAuth state.

Global options:

- `--auth-file`
- `--client-id`
- `--refresh-skew-seconds`

Subcommands:

| Subcommand | Arguments/options | Purpose |
|---|---|---|
| `status` | `--json` | Show auth status and expiry |
| `refresh` | `--force` | Refresh bearer token |
| `token` | none | Print a fresh bearer token |

### `jc migrate-to-0.3`

Binary: `bin/jc-migrate-to-0.3`

Purpose: one-shot migration helper for older instances.

Options:

- `--instance-dir`
- `--triage none|openrouter|ollama|claude-channel`
- `--default-brain`
- `--telegram-chat-id`
- `--enable-slack`
- `--enable-discord`
- `--dry-run`

### `jc completion`

Binary: `bin/jc-completion`

Purpose: print shell completion scripts for `jc`.

Usage:

```bash
jc completion bash
jc completion zsh
```

Install examples:

```bash
jc completion bash > ~/.local/share/bash-completion/completions/jc
jc completion zsh > ~/.zsh/completions/_jc
```

## Cross-Cutting Cleanup Signals

These are catalog observations, not fixes yet:

1. Help behavior was normalized for the worst Bash offenders (`jc-init`,
   `jc-update`, `jc-upgrade`, `jc-doctor`).
2. Top-level help says "all subcommands accept `--instance-dir`", but
   `jc-codex-auth` is not instance-scoped.
3. Namespaces overlap: `jc chats approve --email` now overlaps
   `jc email senders trust`, and `jc gateway list` overlaps `jc gateway events`.
4. Internal commands are still callable where needed, but `jc workers _run` is
   hidden from normal help.
5. Setup/lifecycle commands use mixed strategies: `jc setup`, `jc init`,
   `jc upgrade`, and `jc migrate-to-0.3` each own separate config-writing logic.
6. Directly running Python binaries outside the installed venv can fail before
   help renders because imports happen before argparse setup.
