# JuliusCaesar

An [OpenClaw](https://openclaw.com)-inspired assistant framework built **natively** on [Claude Code](https://www.anthropic.com/claude-code).

> Status: **0.3.0 foundation in progress.** Usable for a single-instance personal assistant, with the first unified-gateway queue/runtime primitives landing. See [ROADMAP.md](./ROADMAP.md).

## Why

OpenClaw proved that a personal AI assistant works best when it's a daemon, not a chat app: persistent memory, multi-channel I/O, cron-driven workflows, a pluggable skill system. But OpenClaw simulates Claude Code over the API, which violates Anthropic's policies for subscription users and creates stability issues when the upstream evolves.

**JuliusCaesar takes the architecture and runs it on the real `claude` CLI** — the user installs Claude Code and signs in with their own subscription; JuliusCaesar orchestrates processes around it. No API simulation, no session spoofing, no TOS concerns.

## Design

- **Framework** (this repo): scheduler, supervisor, memory CLI, voice, installer. No user data.
- **Instance** (separate private repo per user): identity, memory contents, skill configs, credentials. Owned and controlled by the user.

An instance directory is the "workspace." `jc setup` configures one for first run, `jc init` is the low-level scaffold, and `jc <subcommand>` reads config from the current instance and invokes framework tooling.

## Quick start

```bash
git clone https://github.com/matsei-ruka/juliuscaesar ~/juliuscaesar
cd ~/juliuscaesar && ./install.sh
jc setup ~/my-assistant
cd ~/my-assistant
jc heartbeat run hello --dry-run
jc doctor --fix
jc gateway status
# start live runtime when ready:
claude --dangerously-skip-permissions --chrome --channels plugin:telegram@claude-plugins-official
```

Full walkthrough: [QUICKSTART.md](./QUICKSTART.md).

## Components (shipped)

- `jc memory`     — llm-wiki + SQLite FTS5 knowledge base with L1/L2 cache split (Karpathy's LLM Wiki pattern)
- `jc heartbeat`  — YAML-driven task runner, cron-triggered, per-task tool+model routing (claude, gemini, opencode, minimax), with pre_fetch → hash-delta → synthesis pipeline and MCP-independent Telegram delivery
- `jc voice`      — TTS + ASR + enrollment via DashScope Qwen (Singapore/intl endpoint)
- `jc watchdog`   — supervisor for the live `claude` session. Detects claude auto-update crashes AND telegram plugin deaths, restarts with `--resume` so conversation memory survives
- `jc gateway`    — unified-gateway foundation: local SQLite event queue, daemon lifecycle (`start`, `stop`, `restart`, `status`, `tail`), event enqueue/claim/complete/fail primitives
- `jc init`       — scaffold a new instance from `templates/init-instance/`
- `jc setup`      — guided first-run configurator that writes `.env`, L1 memory, watchdog config, and runs diagnostics
- `jc doctor`     — pre-flight checks (binaries, instance structure, credentials, runtime, gateway) plus `--fix` for conservative local repairs
- `jc`            — top-level router

## Components (planned — 0.2.0+)

- `jc skill`      — declarative SKILL.md manifests, install/uninstall/list
- More channels: Discord, Slack
- CI (lint, shellcheck, pytest)
- Docs site

See [ROADMAP.md](./ROADMAP.md).

## Contracts

- **Instance dir resolution** (same for every `jc-*` binary): `--instance-dir <path>` → `$JC_INSTANCE_DIR` → walk up for a `.jc` marker → cwd.
- **Secrets live in `<instance>/.env`**, mode 600. Never in the framework repo.
- **SQLite FTS5 index is derived**, never authoritative. Rebuild from the markdown files with `jc memory rebuild`.
- **Gateway queue is runtime state**, stored at `<instance>/state/gateway/queue.db`. Initialize with `jc gateway init` or `jc doctor --fix`.
- **Adapter contract**: framework adapters are stdin → stdout shell scripts. Model passed as `$1`. `tasks.yaml` points at them by name.
- **Framework has no knowledge of specific instances** — everything flows through `instance_dir`.

## Architecture

[docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md).

## License

[MIT](./LICENSE).

## Credits

- Pattern: [Karpathy's LLM Wiki](https://karpathy.bearblog.dev/llm-wiki/) (memory layer)
- Pattern: [OpenClaw](https://openclaw.com) (assistant daemon architecture)
- Built with [Claude Code](https://www.anthropic.com/claude-code)
