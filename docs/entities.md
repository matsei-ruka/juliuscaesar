# Entities — operator guide

A practical how-to for enabling the relational awareness layer (`memory/L2/entities/`) on a JC instance. For the technical design, see [`docs/specs/relational-awareness-layer.md`](./specs/relational-awareness-layer.md).

## What this is

A typed, categorized substrate for the **people, agents, organizations, and collectives** the agent interacts with. Each entity gets one Markdown file with structured frontmatter (category, knowledge state, confidence) and a free-form body. The agent reads records on demand via normal L2 retrieval; the framework only surfaces a one-line pointer in the gateway preamble when the feature is on.

It replaces ad-hoc `memory/L2/people/` notes — free-form, no schema, inconsistent across instances — with a substrate that the inter-agent protocol and adaptive-discovery discipline both depend on.

## When to enable

Enable for instances where the agent regularly interacts with the same humans, peer agents, or organizations and needs a stable read of who they are:

- Role-shaped personas with recurring stakeholders (clients, vendors, peer agents).
- Multi-agent ecosystems where peer identity matters (see [`docs/inter-agent-protocol.md`](./inter-agent-protocol.md)).
- Instances using the adaptive-discovery discipline (see [`docs/adaptive-discovery.md`](./adaptive-discovery.md)) — that discipline reads `knowledge_state` and `classification_confidence` directly from entity records.

Skip it (or keep it disabled — the default) for:

- Single-purpose bots with no relational state.
- Demo / experimental instances.
- Instances where contacts are transient and a free-form note suffices.

## How to opt in (step by step)

1. **Set the config flag.** Edit `ops/gateway.yaml`:

   ```yaml
   entities:
     enabled: true
     migrate_legacy_people: false    # set true once via the scaffolder; the subcommand flips it
   ```

   *(Phase 2 of the spec lands the validator. Until then, the flag is read but not validated.)*

2. **Scaffold the templates.** From your instance directory:

   ```bash
   jc memory scaffold entities
   jc memory scaffold entities --migrate-people    # one-shot, non-destructive migration
   ```

   *(Phase 3. Until that subcommand lands, copy the templates manually from `<framework>/templates/instance/memory/L2/entities/`.)*

   The scaffold creates `memory/L2/entities/{_README.md, _categories.md, <slug>.md.template}`. The migration form moves existing `memory/L2/people/*.md` to `memory/L2/_archive/people-pre-<YYYY-MM-DD>/` and seeds one stub record per former file with conservative defaults (`unknown / inferred / low`).

3. **Write entity records.** Copy `<slug>.md.template` to `<your-slug>.md` and fill in the frontmatter and body. New contacts default to `entity_category: unknown` — promote later when evidence arrives.

4. **Rebuild the FTS index.**

   ```bash
   jc memory rebuild
   ```

5. **Verify.** Run `jc-doctor` and look for the "Entities" section (Phase 5).

## How to write a good entity record

- **Use the six-category enum.** See `templates/instance/memory/L2/entities/_categories.md` for one anonymous example per category. If you're unsure between `internal_authority` and `internal_peer`, it's almost always `internal_peer`.
- **Pick the right knowledge state.** `declared` is for claims that came from an authoritative source (the principal, the entity itself in a verified channel, a signed document). `inferred` is for claims the agent deduced. `hybrid` is the honest answer most of the time — mark per-attribute provenance inline in the body with `[declared]` / `[inferred]` tags.
- **`confidence_basis` is a one-liner.** Describe *why* the classification is justified: "Principal confirmed role on 2026-04-21" or "Inferred from sign-off pattern across three procurement threads". Not a judgment of the entity's character.
- **`last_verified` is operational.** Update it whenever the operator (or the agent, with confirmation) re-checks the record. Stale `last_verified` is a signal that the record needs re-reading.
- **Cross-link.** Use wiki-links from `RULES.md`, `HOT.md`, or other L2 files: `[[entities/<slug>]]`. The existing rewriter resolves them; no extra setup.

## Promoting `unknown` → categorized

The agent does NOT promote on its own. Reclassification is gated by the **accountabilities authority flow** (v1 reuse): the configured `accountabilities.authority_channel` + `accountabilities.enactment_token` enact a category change.

In practice: the agent proposes the promotion in chat ("I'd classify Diego as `external_vendor` based on X, Y, Z — confirm?"). The operator replies with the configured token. The agent updates the frontmatter (`entity_category`, `knowledge_state`, `classification_confidence`, `confidence_basis`, `last_verified`) and commits. Draft notes from any channel are fine; the enactment is the gate.

## Gotchas

- **Don't preload a roster.** Entities are per-instance content. Start with the records you actually need; let the rest accrete.
- **Don't skip the `unknown` step.** Even when the agent has a strong hypothesis on Day 1, the right path is `unknown / inferred / low` → observe → propose promotion → enact. Skipping the step erodes the discipline the adaptive-discovery section depends on.
- **Don't conflate declared and inferred in the same sentence.** When `knowledge_state: hybrid`, mark per-attribute provenance inline with `[declared]` / `[inferred]`. The discipline only works if the marks are honest.
- **Don't migrate `people/` blind.** The `--migrate-people` form is non-destructive (archive, not delete) and seeds conservative defaults — but the resulting records still need operator review before they're trustworthy.
- **`confidence_basis` is freeform; keep it factual.** No suspicions, no judgments. The field describes evidence, not opinion.

## Where to go deeper

- Full design + phases: [`docs/specs/relational-awareness-layer.md`](./specs/relational-awareness-layer.md).
- Templates: `templates/instance/memory/L2/entities/<slug>.md.template`, `_README.md`, `_categories.md`.
- Related: [`docs/inter-agent-protocol.md`](./inter-agent-protocol.md), [`docs/adaptive-discovery.md`](./adaptive-discovery.md).
