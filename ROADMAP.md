# Roadmap

## 0.1.0 — "It walks" ✅ shipped 2026-04-23

Extract the self-contained pieces from the Rachel reference instance into a working framework.

- [x] `bin/jc` — CLI router with subcommand dispatch (#4)
- [x] `jc init` — scaffold a new instance dir from `templates/init-instance/` (#4)
- [x] `jc memory` — extract rachel_zane/memory/cli → lib/memory + bin/jc-memory (#1)
- [x] `jc heartbeat run <task>` — extract rachel_zane/heartbeat → lib + bin (#2)
- [x] `jc voice speak <text>` — extract rachel_zane/voice → lib + bin (#3)
- [x] `jc watchdog install` — extract rachel_zane/ops/watchdog.sh → bin + config template (#4)
- [x] `install.sh` — one-liner install to `~/.local/bin/` (#1, refined through #4)
- [x] Smoke tests per component (validated during each PR)
- [x] Rachel instance migrated to consume JC 0.1 (3 rachel_zane commits mirror the PRs)

## 0.1.1 — Hardening for external users ✅ shipped 2026-04-23

- [x] Plugin-death watchdog — detect telegram-plugin-dead-but-claude-alive and restart (#5)
- [x] `QUICKSTART.md` — zero-to-first-ping walkthrough (#5)
- [x] `jc doctor` — 29-check pre-flight (#5)

## 0.2.0 — "It runs"

- [x] Codex adapter (GPT-5.x via ChatGPT subscription) — #10
- [x] `jc workers` — on-demand background agents (spec: `docs/specs/workers.md`)
- [ ] Skill loader: `jc skill install <name>`, `jc skill list`, declarative SKILL.md manifests
- [ ] Instance templates beyond the base: `jc init --template=minimal|full|briefings-only`
- [ ] Channel plumbing abstraction beyond Telegram (Discord or Slack)
- [ ] `jc upgrade` self-updater
- [ ] CI (shellcheck, pytest, smoke test of `jc init` → `jc doctor` pipeline)
- [x] `install.sh`: refuse to overwrite shims that point at a different repo
- [ ] First external alpha tester

## 0.3.0 — "It helps"

- [ ] Config schema validator
- [ ] Documentation site
- [ ] Consolidation / "auto-dream" for L1 HOT.md pruning
- [ ] Second production instance (not Rachel)

## 1.0.0 — "It's public"

- [ ] Go public. GitHub repo flipped to public.
- [ ] npm / brew / curl install distribution
- [ ] Tutorial and examples
- [ ] Contributing guide, issue templates

## Not yet scheduled

- Web UI
- Hosted / managed instances
- Multi-user / team instances
- Semantic memory layer (sqlite-vec)
