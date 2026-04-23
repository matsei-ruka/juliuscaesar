# Roadmap

## 0.1.0 — "It walks"
Extract the self-contained pieces from the Rachel instance into a working framework.

- [ ] `bin/jc` — CLI router with subcommand dispatch
- [ ] `jc init` — scaffold a new instance dir from `templates/init-instance/`
- [ ] `jc memory` — extract rachel_zane/memory/cli → lib/memory + bin/jc-memory
- [ ] `jc heartbeat run <task>` — extract rachel_zane/heartbeat → lib + bin
- [ ] `jc voice speak <text>` — extract rachel_zane/voice → lib + bin
- [ ] `jc watchdog install` — extract rachel_zane/ops/watchdog.sh → bin + config template
- [ ] `install.sh` — one-liner install to `~/.local/bin/`
- [ ] Minimal smoke tests per component
- [ ] Rachel instance migrated to consume JC 0.1

## 0.2.0 — "It runs"
- [ ] Skill loader: `jc skill install <name>`, `jc skill list`, declarative SKILL.md manifests
- [ ] Instance templates beyond the base: `jc init --template=minimal|full|briefings-only`
- [ ] Channel plumbing abstraction (Telegram + at least one more: Discord or Slack)
- [ ] `jc upgrade` self-updater
- [ ] CI (lint, shellcheck, pytest)

## 0.3.0 — "It helps"
- [ ] Config schema validator
- [ ] `jc doctor` — diagnose common setup issues
- [ ] Documentation site
- [ ] First external alpha tester (not Luca)

## 1.0.0 — "It's public"
- [ ] Go public. GitHub repo flipped to public.
- [ ] npm / brew / curl install distribution
- [ ] Tutorial and examples

## Not yet scheduled
- Web UI
- Hosted / managed instances
- Multi-user / team instances
