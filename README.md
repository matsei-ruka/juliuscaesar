# JuliusCaesar

An OpenClaw-inspired assistant framework built **natively** on Claude Code.

> Status: **0.0.0 — scaffolding.** Not yet usable. See [ROADMAP.md](./ROADMAP.md).

## Why

[OpenClaw](https://openclaw.com) proved that a personal AI assistant works best when it's a daemon, not a chat app: persistent memory, multi-channel I/O, cron-driven workflows, a pluggable skill system. But OpenClaw simulates Claude Code over the API, which violates Anthropic's policies for subscription users and creates stability issues when the upstream evolves.

**JuliusCaesar takes the architecture and runs it on the real `claude` CLI** — the user installs Claude Code and signs in with their own subscription; JuliusCaesar orchestrates processes around it. No API simulation, no session spoofing, no TOS concerns.

## Design

- **Framework** (this repo): scheduler, supervisor, memory CLI, voice, skill loader, channel bus. No user data.
- **Instance** (separate private repo per user): identity, memory contents, skill configs, credentials. Owned and controlled by the user.

An instance directory is the "workspace." `jc init` scaffolds one. `jc <subcommand>` reads config from the current instance, invokes framework tooling.

## Components (planned)

- `jc memory`     — llm-wiki + SQLite FTS5 knowledge base with L1/L2 cache split
- `jc heartbeat`  — YAML-driven task runner; cron-triggered; per-task tool+model routing (claude, gemini, opencode, minimax)
- `jc voice`      — TTS + ASR via DashScope Qwen (cloned voice)
- `jc watchdog`   — supervisor for the live `claude` session (survives auto-updates)
- `jc channel`    — Telegram / Discord / Slack / email via MCP plugins
- `jc skill`      — install/uninstall/list instance skills

## Not yet

- CLI router (`bin/jc`)
- Any of the components above
- Install script
- Tests, CI, docs

## License

[MIT](./LICENSE).

## Credits

- Pattern: [Karpathy's LLM Wiki](https://x.com/karpathy/status/...) (memory layer)
- Pattern: [OpenClaw](https://openclaw.com) (assistant daemon architecture)
- Built with [Claude Code](https://www.anthropic.com/claude-code).
