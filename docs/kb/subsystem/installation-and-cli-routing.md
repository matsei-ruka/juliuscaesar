---
title: Installation and CLI routing
section: subsystem
status: active
code_anchors:
  - path: install.sh
    symbol: "BINARIES=("
  - path: bin/jc
    symbol: "case \"$SUB\" in"
last_verified: 2026-05-01
verified_by: l.mattei
related:
  - contract/instance-layout-and-resolution.md
---

## Summary

`install.sh` installs JuliusCaesar by creating a venv at `~/.local/share/juliuscaesar/venv`, installing Python dependencies, and writing executable shims into `~/.local/bin`. The shims call the binaries in the current framework checkout, so `git pull` updates behavior without reinstalling.

The top-level `jc` command is a bash router. It dispatches to matching `jc-*` binaries on PATH. Current subcommand surface (router help in `bin/jc:usage()`):

- Core: `memory`, `heartbeat`, `voice`, `watchdog`, `workers`, `gateway`, `init`, `setup`, `doctor`, `completion`.
- Lifecycle: `update` (CalVer framework upgrade), `upgrade` (reconfigure existing instance: channels, brain, triage), `migrate-to-0.3` (one-shot migration helper for 0.2.x instances).
- Conversation surface: `chats` (Telegram chat directory), `email` (email channel operations), `transcripts` (per-conversation chat history read/tail/search).
- Observability: `company` (fleet observability client — see `lib/company/`).
- Modeling: `user-model` (autonomous user-model corpus/detector/proposer/applier).
- Auth: `codex-auth` (inspect/refresh local Codex CLI OAuth state used by the `codex_api` brain).

## Source of truth

- `install.sh` owns dependency setup and shim generation.
- `bin/jc` owns the public router surface, including the first-class `email`
  subcommand that dispatches to `jc-email`.
- Individual binaries own subcommand behavior.
- `bin/jc-setup` owns the guided first-run configurator.

## Important behavior

- Python dependencies are currently `pyyaml`, `python-dotenv`, `dashscope`, `requests`, and `websocket-client`.
- Python 3.10+ is required because the library code uses modern type syntax.
- `jc doctor` uses the installed JuliusCaesar venv Python for internal dependency probes and SQLite/YAML helper snippets when that venv exists.
- The installer refuses to overwrite existing `~/.local/bin/jc-*` shims that point to a different JuliusCaesar clone unless run with `--force`.
- Python binaries run through the venv wrapper with the framework `lib/` on `PYTHONPATH`.
- Native bash binaries are invoked directly by their shim.
- The router preserves `--instance-dir <path>` or `--instance-dir=<path>` when placed before the subcommand.
- `jc setup` uses `jc init` underneath, detects logged-in coding tools,
  selects a default brain, configures Telegram as the first communication
  channel, waits for the first message unless `--no-wait` is passed, writes
  `.env` plus `ops/gateway.yaml`, creates bootstrap L1 memory, and rebuilds the
  memory index.
- `jc completion` prints bash/zsh shell completion scripts.
- `jc gateway` owns the unified gateway surface: durable SQLite queue initialization, daemon lifecycle, Telegram/Slack polling, brain dispatch, enqueue, claim, complete/fail, retry, config, logs, and status/list inspection.
- `jc email` owns email-channel operations: doctor, IMAP/SMTP credential
  checks, sender policy, pending inbound inspection/drain, and outbound draft
  review.
- `jc doctor --fix` performs conservative local repairs: chmod `.env` to 600, rebuild a missing memory index, initialize the gateway queue, create missing gateway config, remove stale gateway pidfiles, remove stale legacy Telegram plugin pidfiles, and create `state/`. It also reports email-channel credential presence and local pending/draft metrics.

## Failure modes

- If `~/.local/bin` is missing from PATH, installation still writes shims but warns the user.
- If a different clone already owns the shims, install fails until the user chooses the existing clone or forces overwrite.
- If `jc` cannot find `jc-<subcommand>` on PATH, it exits 127 and tells the user to run `install.sh`.
- The router must handle subcommands with no global flags on macOS Bash under `set -u`; empty global arg arrays are not expanded unconditionally.
- Bash command help paths should return usage for `-h`, `--help`, and `help`;
  this is covered for `jc-init`, `jc-update`, `jc-upgrade`, and `jc-doctor`.
- If the target for `jc init` is non-empty, it refuses portably on macOS and Linux, except for `.git`, `.gitignore`, `README.md`, and `LICENSE`.

## Open questions / known stale

- 2026-04-25: Roadmap still lists public distribution via npm, brew, or curl as future work.
- 2026-05-01: `bin/jc` declares a `VERSION` constant (CalVer, currently `2026.04.28`) used by `jc update` to compare against released framework versions.
