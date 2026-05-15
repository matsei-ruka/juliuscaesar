---
slug: entities-readme
title: Entities — L2 directory README
layer: L2
type: framework-readme
state: active
tags: [entities, framework, readme]
---

# Entities — L2 directory

## What this directory is for

This directory holds one Markdown file per **entity** the agent interacts with — humans, peer agents, organizations, collectives. It replaces ad-hoc `memory/L2/people/` notes with a typed, categorized, versioned substrate that the inter-agent protocol and the adaptive-discovery discipline both depend on.

The agent reads these records on demand via normal L2 retrieval (`jc memory search`, `jc memory read <slug>`). The framework does NOT parse them at runtime — but it does surface a one-line pointer in the gateway preamble when `entities.enabled: true` so the agent knows the substrate exists.

## The six categories

Closed enum, defined in `lib/memory/entities/categories.py`:

| Category               | Meaning                                                                        |
|------------------------|--------------------------------------------------------------------------------|
| `internal_authority`   | The principal and anyone with explicit authority over the agent.               |
| `internal_peer`        | Colleagues at the agent's level — human or agent — inside the org.             |
| `external_client`      | Clients the agent serves on behalf of the principal.                           |
| `external_vendor`      | Suppliers / partners providing services to the principal.                      |
| `external_occasional`  | One-off external contacts (intros, networking, transient).                     |
| `unknown`              | Entity exists but not yet classified. Default for new contacts.                |

See `_categories.md` in this directory for one anonymous example per category.

## The three knowledge states

Every entity record carries a `knowledge_state` in the frontmatter:

- **`declared`** — every claim came from an authoritative source (the principal, the entity itself in a verified channel, a signed document). Treated as fact.
- **`inferred`** — claims were deduced by the agent from observation. Treated as hypothesis. The `classification_confidence` field qualifies the hypothesis.
- **`hybrid`** — mixed. Some attributes are declared, others inferred.

When `knowledge_state: hybrid`, mark per-attribute provenance inline in the body with `[declared]` or `[inferred]` tags. Example:

```markdown
## Identity

- Procurement lead at <Org> [declared, principal 2026-04-21]
- Handles vendor negotiations end-to-end [inferred, sign-off pattern across three threads]
```

The frontmatter `knowledge_state` is the *dominant* state; per-attribute provenance lives in the body.

## How to add a record

1. Copy `<slug>.md.template` to `<your-slug>.md` (kebab-case; the slug becomes the L1/L2 cross-reference key).
2. Fill in the frontmatter. New contacts default to `entity_category: unknown`, `knowledge_state: inferred`, `classification_confidence: low`. Promote later when evidence arrives.
3. Fill the body sections (`## Identity`, `## Relationship`, `## Communication`, `## Open items`, `## Notes`). Empty placeholders (`…`) are fine while drafting; missing sections are not.
4. Run `jc memory rebuild` to re-index. The record is now searchable.

## Promoting `unknown` → categorized

Promotion is gated by the **accountabilities authority flow** (v1). Only the configured `accountabilities.authority_channel` + `accountabilities.enactment_token` (see `ops/gateway.yaml`) can enact a category change. Drafts and notes from any channel are fine; reclassification is not.

When evidence resolves an `unknown`, the agent proposes the promotion in chat. The operator confirms with the configured token. The agent then updates the frontmatter (`entity_category`, `knowledge_state`, `classification_confidence`, `confidence_basis`, `last_verified`) and commits.

## Reserved files

- `_README.md` (this file)
- `_categories.md` — anonymous reference of the six categories
- `_audit.md` — reserved for the future migration-audit writer (see spec Phase 6)

Do not create entity records named `_readme`, `_categories`, or `_audit`.

## Naming convention

- kebab-case (`diego-la-torre`, not `DiegoLaTorre` or `diego_la_torre`)
- descriptive — a reader should guess the entity from the slug
- stable — the slug is referenced from L1 (`authority-map.md`), other L2 files, and audit logs
- avoid leading underscore (reserved for framework files)

## Where to go deeper

- Operator guide: `docs/entities.md`
- Full design + phases: `docs/specs/relational-awareness-layer.md`
- Related: `docs/inter-agent-protocol.md`, `docs/adaptive-discovery.md`
