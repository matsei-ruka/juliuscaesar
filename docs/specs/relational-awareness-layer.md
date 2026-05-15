# Spec: Relational Awareness Layer — `memory/L2/entities/`

**Status:** Draft
**Date:** 2026-05-15
**Branch base:** `main`
**Branch:** `spec/multi-agent-awareness`
**Owner:** TBD

## Goal

Give every JC instance a structured, machine-readable substrate for the **people, agents, organizations, and collectives** the agent interacts with. Replaces ad-hoc `memory/L2/people/` notes (free-form Markdown, no schema, often not committed to git) with a typed, categorized, versioned entity store at `memory/L2/entities/`.

Two consumers depend on this substrate downstream:

- The **inter-agent protocol** (separate spec): needs to know which entities are peer agents, which human authority governs each, and where their accountability manifests live.
- The **adaptive-discovery protocol** (separate spec): needs to distinguish *declared* knowledge (verified by an authority) from *inferred* knowledge (deduced by the agent), and to track classification confidence over time.

This is layer 2 of a three-spec stack: accountability manifest (already shipped) → relational awareness layer (this spec) → inter-agent + discovery (separate specs).

## Non-goals

- Do not ship a preloaded entity roster. Entities are per-instance content.
- Do not migrate existing `memory/L2/people/` automatically. Operators trigger migration with `jc memory scaffold entities --migrate-people` and review the result.
- Do not change instance defaults. Instances without `entities/` continue to work; the agent reads `people/` as today.
- Do not replace conversation transcripts (`state/transcripts/`) — entity records are reference state, transcripts are event-time state.
- Do not introduce a runtime cross-instance entity registry. Each instance owns its own `entities/`. Sharing happens at the operator level by deliberate copy/sync.
- Do not extend the FTS5 schema in `lib/memory/db.py` beyond what already indexes Markdown frontmatter — entities are searchable like any L2 doc.

## Background — current state on the reference instance

A single instance ("Mario", `/opt/mario_leone_coo`) runs this scheme. Structure observed:

- `memory/L2/entities/` — one Markdown file per entity, standardized YAML frontmatter, free-form body.
- `memory/L2/_archive/people-pre-2026-05-15/` — non-destructive backup of the original `people/` directory, preserved for audit/rollback.
- 6 entity records at time of writing: 3 `internal_authority`, 2 `internal_peer`, 1 `external_client`.
- Six closed-enum categories (see below).
- Two knowledge states (`declared` / `inferred`) plus a per-entity confidence rating.

No framework code reads `entities/` today. The agent consumes them through normal L2 retrieval (`jc memory search`, `jc memory read <slug>`).

## Desired behavior

### Entity categories (closed enum, v1)

| Category               | Description                                                       |
|------------------------|-------------------------------------------------------------------|
| `internal_authority`   | The instance's principal(s) and anyone with explicit authority over the agent. |
| `internal_peer`        | Colleagues at the agent's level — human or agent — inside the principal's organization. |
| `external_client`      | Clients, contacts the agent serves on behalf of the principal.    |
| `external_vendor`      | Suppliers, partners providing services to the principal.          |
| `external_occasional`  | One-off external contacts (intros, networking, transient).        |
| `unknown`              | Entity exists but is not yet classified. Default for new contacts. |

Categories live in `lib/memory/entities/categories.py` as a frozen tuple. Operators may not redefine the set in v1 (extensibility is in Open questions).

### Entity record format

A Markdown file at `memory/L2/entities/<entity-slug>.md`. Required frontmatter:

```yaml
---
slug: <entity-slug>                  # kebab-case; stable identifier
entity_id: <entity-slug>             # same as slug; reserved for future ID-vs-slug split
entity_type: human | agent | organization | collective
entity_category: internal_authority | internal_peer | external_client |
                 external_vendor | external_occasional | unknown
display_name: <full name as the operator would say it aloud>
human_authority: <slug of the internal_authority that governs this entity, or "" if N/A>
accountabilities_pointer: <relative path to the entity's accountability manifest, or "TBD" or "">
knowledge_state: declared | inferred | hybrid
classification_confidence: high | medium | low
confidence_basis: <one-line justification for the classification_confidence>
created: YYYY-MM-DD
updated: YYYY-MM-DD
last_verified: YYYY-MM-DD
tags: [entities, <category>, <free-form tags>]
---
```

Body is free-form Markdown. Recommended sections:

- `## Identity` — full name, role, organization, languages, location.
- `## Relationship` — how the principal knows them, history, current status.
- `## Communication` — preferred channels, cadence, register, taboos.
- `## Open items` — active threads, pending follow-ups, commitments.
- `## Notes` — operator color, observations, anything that doesn't fit above.

### Knowledge states (declared / inferred / hybrid)

- `declared`: every claim in the record came from an authoritative source (the principal, the entity itself, a signed document, an authority chain). Treated as fact during reasoning.
- `inferred`: claims were deduced by the agent from observation (interactions, message register, contextual hints). Treated as hypothesis. Confidence rating qualifies the hypothesis.
- `hybrid`: some attributes are declared (e.g., name and email confirmed by principal) and others inferred (e.g., communication preferences deduced from chat history). The body should mark per-attribute provenance with `[declared]` / `[inferred]` tags.

The frontmatter knowledge_state is the *dominant* state. Per-attribute provenance lives in the body.

### Archive directory

When an operator runs `jc memory scaffold entities --migrate-people`, the existing `memory/L2/people/` is moved to `memory/L2/_archive/people-pre-<YYYY-MM-DD>/` and a stub `entities/` directory is created with one converted record per former `people/` file plus a `_README.md` listing migration heuristics applied. The original `people/` directory is left empty (not deleted) so any tooling pointing at the old path fails loudly rather than silently.

### Templates

Three templates ship in `templates/instance/memory/L2/entities/`:

- `<slug>.md.template` — an entity record stub with all required frontmatter fields and recommended body sections, placeholders filled with `<...>`.
- `_README.md` — operator guide for the directory (categories, knowledge states, how to add an entity).
- `_categories.md` — reference document enumerating the six categories with examples; not consumed by the framework, lives in the instance as documentation.

### Constitutional anchor in `RULES.md`

The relational awareness layer does not require its own §-numbered constitutional section. The agent treats entity records as L2 knowledge like any other domain reference — the discipline lives in the `adaptive-discovery` spec (`§<N+1> AUTHORITY AWARENESS AND ADAPTIVE DISCOVERY`).

### Authority for entity changes — assimilated into the operator approval flow

Adding, removing, or recategorizing an entity is treated the same as a manifest change in the accountability system: changes route through the configured authority channel + enactment token (reuses `accountabilities.authority_channel` + `accountabilities.enactment_token` until and unless operators want a separate gate). In v1 we *reuse* the accountability authority config; v2 may introduce `entities.authority_*` keys if operators ask.

The principle: only the primary operator (or the configured email sender) can promote an entity from `unknown` → any other category, or move an entity between non-`unknown` categories. Drafts and notes from any channel are fine; reclassification is gated.

### Per-instance override

Instances opt in via `ops/gateway.yaml`:

```yaml
entities:
  enabled: false                        # default false
  migrate_legacy_people: false          # one-time hint; the scaffold subcommand sets this true after success
```

When `entities.enabled: false`, the framework ignores `memory/L2/entities/` even if present. When enabled, the framework injects a one-line preamble pointer (à la the accountability manifest) telling the agent the directory exists; the agent retrieves records on demand via `jc memory`.

### Runtime behavior

- The framework does NOT parse entity records at runtime. The agent reads them through normal L2 retrieval.
- When `entities.enabled: true`, the gateway preamble includes a single line: `Entities directory: memory/L2/entities/ (six categories, see _categories.md).` so the agent knows the structure exists without paying tokens for every record.
- Cross-references from other L1/L2 files (e.g., `[[entities/diego-la-torre]]`) resolve through the existing wiki-link rewriter — no changes needed.

## Implementation plan

### Phase 1 — Templates and docs

- Author `templates/instance/memory/L2/entities/<slug>.md.template`.
- Author `templates/instance/memory/L2/entities/_README.md`.
- Author `templates/instance/memory/L2/entities/_categories.md`.
- Author `docs/entities.md` — operator guide explaining categories, knowledge states, and the migration story.
- Link from `QUICKSTART.md` under "Optional features".

### Phase 2 — Config schema

- Add `EntitiesConfig` dataclass to `lib/gateway/config.py` with `enabled: bool` and `migrate_legacy_people: bool`.
- Wire validation into `_validate_raw_config()` and `allowed_top`.
- Add `entities` field to `GatewayConfig`.
- Tests: `tests/gateway/test_config_env.py::EntitiesSchemaTests` covering enabled/disabled and rejection of unknown keys.

### Phase 3 — `jc memory scaffold entities`

- Add `scaffold_entities(instance_dir, *, migrate_people)` to `lib/memory/scaffolding.py`.
- Copy the three templates to `memory/L2/entities/`.
- When `migrate_people=True`, scan `memory/L2/people/` for `.md` files, move them to `memory/L2/_archive/people-pre-<YYYY-MM-DD>/`, generate stub `entities/<slug>.md` for each (frontmatter only, `knowledge_state: inferred`, `classification_confidence: low`, `entity_category: unknown`), write a `_README.md` in the archive directory recording the migration step.
- Idempotent: existing entity files are skipped with `[skip]`. Migration is a one-shot — the archive directory's existence is the marker.
- Tests: scaffold creates templates, idempotent skip, migration moves files, migration generates stubs with correct defaults.

### Phase 4 — Preamble injection

- Add `render_entities_block(instance_dir) -> str` in `lib/gateway/context.py`. Returns `""` when disabled, otherwise the one-line pointer described above.
- Wire into `render_preamble()` (non-Claude) and into the Claude per-event prefix alongside the accountabilities manifest block.

### Phase 5 — `jc-doctor` entity checks

- Add `lib/health/entities_check.py::check_entities(instance_dir) → list[HealthItem]`. When disabled: single `INFO`. When enabled:
  - `memory/L2/entities/` exists.
  - At least one entity record present (otherwise `INFO: no entities recorded yet`).
  - Each `.md` file parses as Markdown with valid frontmatter.
  - Every frontmatter contains the required fields, with `entity_category` in the closed enum and `knowledge_state` in `{declared, inferred, hybrid}` and `classification_confidence` in `{high, medium, low}`.
  - Slug stability: frontmatter `slug` matches filename stem.
- Wire into `bin/jc-doctor` as a new "Entities" section.

### Phase 6 — Optional: migration audit (deferred)

A separate writer that records every `unknown → <category>` transition in `memory/L2/entities/_audit.md` (same append-only contract as the accountabilities audit). Useful for adaptive-discovery telemetry. Defer until adaptive-discovery is in flight.

### Phase 7 — End-to-end smoke

Manual operator work: scaffold a fresh instance, run `jc memory scaffold entities --migrate-people`, write or migrate ≥3 records, verify `jc-doctor` is green, confirm cross-references from L1/L2 resolve.

## Backward compatibility

Instances without `entities/` continue to work. Instances with `memory/L2/people/` are not modified until the operator runs `jc memory scaffold entities --migrate-people`. The migration is non-destructive (archive, not delete). Existing wiki-links `[[people/<slug>]]` keep working until the operator chooses to rewrite them.

## Security and safety

- Entity records can contain personal data. Recommend operators keep `entities/` in a private repo (default JC pattern) and avoid committing personally identifying information of clients/vendors without consent.
- `entity_category` is an authority decision; chat-level enactment is gated by the accountability authority flow.
- `confidence_basis` is freeform — operators must avoid documenting suspicions or judgments. The field describes *why this classification is justified*, not opinions about the entity.

## Open questions

- **Custom categories.** v1 enum has six values. v2 might add `auditor`, `regulator`, `ex_employee`. Pattern: extend the tuple, document migration. Open question: do operators want a `memory/L2/entities/_categories_ext.yaml` for per-instance additions, or framework-level enum bumps only?
- **`entity_type: collective` vs `organization`.** Mario uses `organization` for companies and `collective` for informal groups (e.g., a recurring discussion circle). Worth keeping both? Could collapse into `organization` with a body tag.
- **Cross-instance entity sharing.** Operators with multiple JC instances (e.g., one per role) often see the same external clients. v1 leaves this as a copy-paste exercise; v2 could offer `jc entities export <slug>` / `jc entities import <slug>`.
- **Confidence decay.** A `high` classification from 2024 may not be `high` in 2026. v1 leaves confidence as a snapshot; v2 could attach `confidence_until: YYYY-MM-DD` for re-verification cadence.

## Definition of done

- Templates + operator docs ship.
- `EntitiesConfig` validated and wired into `GatewayConfig`.
- `jc memory scaffold entities` works and is idempotent.
- `jc-doctor` reports entity health when enabled, single `INFO` when disabled.
- One reference instance (Rachel or a fresh test instance) successfully scaffolds + migrates + passes doctor.
- KB entry at `docs/kb/subsystem/entities.md`.
- Spec, code, tests, and KB entry land on `main` behind the opt-in flag.

## Rollout plan

1. Land spec.
2. Phases 1–5 on `feat/relational-awareness-layer`, one commit per phase.
3. Open PR with a test plan, request operator review.
4. Merge, tag as `v2026.05.YY.NN` per JC release cadence.
5. Operators opt in per-instance; legacy `people/` migration is opt-in via the scaffold flag.
