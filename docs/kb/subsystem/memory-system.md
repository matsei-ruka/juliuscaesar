---
title: Layered memory system
section: subsystem
status: active
code_anchors:
  - path: bin/jc-memory
    symbol: "Subcommands:"
  - path: lib/memory/db.py
    symbol: "CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts"
  - path: templates/init-instance/CLAUDE.md
    symbol: "@memory/L1/IDENTITY.md"
last_verified: 2026-04-25
verified_by: l.mattei
related:
  - contract/instance-layout-and-resolution.md
---

## Summary

The instance memory system follows an L1/L2 split. L1 is small and always loaded into Claude Code sessions through the instance `CLAUDE.md`. L2 is a larger markdown vault searched on demand through SQLite FTS5.

Markdown files are the source of truth. `memory/index.sqlite` and `memory/INDEX.md` are derived and rebuilt from the markdown files.

## Source of truth

- L1 files: `<instance>/memory/L1/*.md`
- L2 files: `<instance>/memory/L2/**/*.md`
- CLI: `bin/jc-memory`
- Parser and SQLite/FTS5 index: `lib/memory/db.py`
- Instance import template: `templates/init-instance/CLAUDE.md`

## CLI surface

`jc memory` supports:

- `new`: create an L1 or L2 entry.
- `write`: replace an entry body.
- `read`: print one entry and mark it accessed.
- `search`: run FTS5 ranked search and print snippets.
- `link`: append a wikilink.
- `lint`: detect broken wikilinks, orphans, and stale entries.
- `log`: tail `memory/LOG.md`.
- `rebuild`: rescan markdown, sync SQLite, and rewrite `memory/INDEX.md`.
- `consolidate`: placeholder for future auto-dream behavior.

## Data model

`lib/memory/db.py` stores entries with slug, title, layer, type, state, path, dates, tags, body, and backlinks. FTS5 indexes title, tags, and body. Wikilinks in bodies plus explicit `links` frontmatter populate the backlinks table.

## Invariants

- L1 slugs are simple names under `memory/L1`.
- L2 slugs include their directory path under `memory/L2`.
- Markdown frontmatter is required for indexed files.
- The DB can be deleted and rebuilt from markdown.

## Open questions / known stale

- 2026-04-25: `consolidate` is still a placeholder, and roadmap lists L1 HOT pruning / auto-dream as future work.
