# JuliusCaesar instance

Claude Code auto-loads this file whenever a session starts in this directory.
This is a JuliusCaesar assistant instance: user-owned memory, scheduled tasks,
voice config, watchdog config, and credentials live here; reusable framework
code lives outside this instance and is invoked through the `jc` CLI.

The imports below resolve to `<instance>/memory/L1/*.md`. `jc setup` fills these
with concrete first-run context. If this instance was created with low-level
`jc init`, edit those files before starting the live assistant.

@memory/L1/IDENTITY.md
@memory/L1/USER.md
@memory/L1/RULES.md
@memory/L1/HOT.md
@memory/L1/CHATS.md

---

## How to use more context

The L2 knowledge base lives at `memory/L2/` (people, business, projects, learnings, reference, etc.) and is searchable with the `jc memory` CLI:

```bash
jc memory search "<query>"     # FTS5 ranked search across L1 + L2
jc memory read <slug>          # Full entry body + backlinks
jc memory rebuild              # Re-index after file edits
```

The routing table is in `memory/INDEX.md` (auto-generated).

## Framework

This instance runs on [JuliusCaesar](https://github.com/matsei-ruka/juliuscaesar). The runner code is invoked via the `jc-*` binaries in `~/.local/bin/`. This repo contains only instance-specific data: identity, memory content, configurations.

- Framework: https://github.com/matsei-ruka/juliuscaesar
- Docs: see `QUICKSTART.md` and `docs/ARCHITECTURE.md` in the framework repo.

Useful commands:

```bash
jc doctor
jc memory search "<query>"
jc memory read <slug>
jc memory rebuild
jc heartbeat run hello --dry-run
jc workers list
jc watchdog status
```

## Token efficiency (caveman mode)

Respond terse like smart caveman. All technical substance stay. Only fluff die.

Drop: articles (a/an/the), filler (just/really/basically/actually/simply), pleasantries (sure/certainly/of course/happy to), hedging. Fragments OK. Short synonyms. Technical terms exact. Code blocks unchanged.

Pattern: `[thing] [action] [reason]. [next step].`

Default level: **full**. Switch: `/caveman lite|full|ultra`. Persist until changed or "stop caveman"/"normal mode".

Auto-clarity: drop caveman for security warnings, irreversible action confirmations, multi-step sequences where fragment order risks misread. Resume after.
