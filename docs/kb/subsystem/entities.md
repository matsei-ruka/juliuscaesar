---
title: Relational awareness — entities directory
section: subsystem
status: active
last_verified: 2026-05-15
verified_by: Rachel Zane
code_anchors:
  - path: lib/gateway/config.py
    symbol: EntitiesConfig
  - path: lib/gateway/context.py
    symbol: render_entities_block
  - path: lib/gateway/brains/claude.py
    symbol: render_entities_block
  - path: lib/memory/scaffolding.py
    symbol: scaffold_entities
  - path: lib/health/entities_check.py
    symbol: check_entities
  - path: bin/jc-doctor
  - path: tests/gateway/test_context.py
    symbol: EntitiesBlockTests
  - path: tests/health/test_entities_check.py
related:
  - subsystem/accountabilities.md
  - subsystem/inter-agent-protocol.md
  - subsystem/adaptive-discovery.md
  - subsystem/memory-system.md
sources:
  - path: docs/specs/relational-awareness-layer.md
    title: Relational awareness layer spec (P1–P7)
---

# Relational awareness — entities directory

## What it is

Opt-in L2 directory (`memory/L2/entities/`) where the operator records every external/internal entity the agent interacts with. Each record carries a closed-enum `entity_category`, a `knowledge_state` (declared/inferred/hybrid), a `classification_confidence`, and a one-line `confidence_basis`. The framework injects a single-line pointer into the brain preamble when `entities.enabled: true`; agent reads records on demand via `jc memory`.

Shipped across phases 1–5 on branch `spec/multi-agent-awareness`. Phase 6 (migration audit) deferred. Phase 7 (smoke) verified on a fresh test instance — 4/4 entity checks green.

## Key invariants

1. **Feature is opt-in.** `entities.enabled` defaults to `false`. When disabled, `render_entities_block()` returns `""` and `check_entities()` emits a single `INFO`.
2. **Closed enum for `entity_category`.** Six values: `internal_authority`, `internal_peer`, `external_client`, `external_vendor`, `external_occasional`, `unknown`. The doctor warns on anything else.
3. **Closed enum for `knowledge_state`.** Three values: `declared`, `inferred`, `hybrid`. Anything else → warn.
4. **Closed enum for `classification_confidence`.** Three values: `high`, `medium`, `low`. Anything else → warn.
5. **Slug stability.** Frontmatter `slug` must match filename stem. Doctor warns on mismatch.
6. **Underscore files are framework reserved.** `_README.md`, `_categories.md`, `_archive/` etc. are skipped by the entity-record scanner and never validated as entities.
7. **Framework does not parse records at runtime.** The agent retrieves on demand via L2 search; no record-level type-checking on the hot path.

## Architecture

```
ops/gateway.yaml
  entities:
    enabled: true
    migrate_legacy_people: false     # one-shot hint flipped by scaffold --migrate-people
         │
         ▼
lib/gateway/config.py → EntitiesConfig (frozen dataclass)
         │
         ├── lib/gateway/context.py
         │     render_entities_block(instance_dir)
         │     → one-line pointer in render_preamble() + Claude per-event prefix
         │
         ├── lib/memory/scaffolding.py
         │     scaffold_entities(instance_dir, *, migrate_people)
         │     → copies templates, optionally archives memory/L2/people/ + stubs entities
         │
         └── lib/health/entities_check.py
               check_entities(instance_dir) → list[HealthItem]
               called by bin/jc-doctor "Entities" section
```

**Directory layout** (all paths relative to `instance_dir`):

```
memory/L2/entities/_README.md                ← operator guide
memory/L2/entities/_categories.md            ← closed-enum reference (six categories)
memory/L2/entities/<slug>.md.template        ← copy template
memory/L2/entities/<slug>.md                 ← one file per entity (operator-authored)
memory/L2/_archive/people-pre-<YYYY-MM-DD>/  ← legacy people/ archive (one-shot)
```

## Mini recipe

**Enable entities on an instance:**

```
1. Run: jc memory scaffold entities
     Optionally --migrate-people to archive memory/L2/people/.

2. Flip ops/gateway.yaml:
     entities:
       enabled: true

3. Copy <slug>.md.template to <new-slug>.md, fill all required frontmatter fields.

4. Run: jc-doctor
     All Entities items should show ✓ ok or single INFO for empty dir.
```

**Required frontmatter fields:**
`slug`, `entity_category`, `knowledge_state`, `classification_confidence`. Doctor warns if any are missing or empty.

## Gotchas

- **`migrate_people=True` is destructive but recoverable.** The legacy `memory/L2/people/` directory is moved (not deleted) to `memory/L2/_archive/people-pre-<date>/`. Existing wiki-links like `[[people/<slug>]]` break until the operator rewrites them or symlinks.
- **`entity_category: unknown` is a valid stable state.** Records may live in `unknown` indefinitely; the adaptive-discovery discipline governs when/how to promote.
- **Slug must be kebab-case to match filename.** Underscores in slug or extension other than `.md` will fail the slug-stability check.
- **PyYAML is required.** Without it, frontmatter validation reports `PyYAML not installed` per record.
- **Cross-references rely on the existing wiki-link rewriter.** No special-case logic for `[[entities/<slug>]]` — they resolve the same way as any other L2 link.

## Open questions / known stale

- **2026-05-15**: P6 (migration audit recording `unknown → <category>` transitions) deferred. Useful for adaptive-discovery telemetry; tracked in spec Open questions.
- **2026-05-15**: Custom categories (v2 might add `auditor`, `regulator`, `ex_employee`) tracked in spec Open questions.

## See also

- `subsystem/inter-agent-protocol.md` — peer-agent identity references entity records
- `subsystem/adaptive-discovery.md` — confidence-basis discipline operates over entity records
- `subsystem/memory-system.md` — L1/L2 layout that hosts the directory
- `docs/specs/relational-awareness-layer.md` — full spec with all 7 phases
