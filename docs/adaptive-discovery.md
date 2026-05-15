# Adaptive Discovery — operator guide

A practical how-to for enabling the adaptive-discovery discipline on a JC instance. For the technical design, see [`docs/specs/adaptive-discovery.md`](./specs/adaptive-discovery.md).

## What this is

A constitutional discipline with two components:

1. **Knowledge state distinction.** Every load-bearing claim the agent makes about an entity carries an explicit provenance: `declared` (verified by an authority) or `inferred` (deduced by the agent). The two are never silently mixed.
2. **Discovery protocol.** When the agent meets an entity it does not recognize, it follows a bounded procedure: classify as `unknown`, apply conservative defaults, observe, form a hypothesis marked `inferred`, decide whether to seek confirmation based on stakes, and update the entity record after confirmation.

The discipline lives in `RULES.md §<N> AUTHORITY AWARENESS AND ADAPTIVE DISCOVERY`. The frame is reinforced at runtime by a short reminder block injected into the gateway preamble when the feature is on. Everything else lives in the agent's reasoning.

## When to enable

Enable when the agent regularly handles **medium- or high-stakes requests from entities of varying familiarity** — i.e., where confusing inferred-with-declared could produce a wrong commitment:

- Role-shaped personas that draft commitments on the principal's behalf.
- Multi-agent ecosystems (paired with [`docs/inter-agent-protocol.md`](./inter-agent-protocol.md)).
- Instances exposed to external channels where new contacts arrive unannounced.

Skip it (or keep it disabled — the default) for:

- Single-purpose bots with no relational state.
- Instances where every interlocutor is fully `declared` (e.g., principal-only).
- Demo / experimental instances.

This discipline depends on the relational awareness layer for the `entities/` substrate and for the `knowledge_state` / `classification_confidence` frontmatter. Turn on `entities` first.

## How to opt in (step by step)

1. **Set the config flag.** Edit `ops/gateway.yaml`:

   ```yaml
   adaptive_discovery:
     enabled: true
     default_unknown_posture: conservative           # only "conservative" supported in v1
     high_stakes_escalation_channel: authority       # routes to accountabilities.authority_channel
   ```

   *(Phase 2 of the spec lands the validator. Until then, the flag is read but not validated.)*

2. **Scaffold the constitutional snippet.** From your instance directory:

   ```bash
   jc memory scaffold adaptive-discovery
   ```

   *(Phase 3. Until that subcommand lands, copy the template manually from `<framework>/templates/instance/memory/L1/RULES.md.adaptive-discovery-section.template`.)*

   The scaffold prints the §-numbered constitutional snippet for you to paste into `RULES.md`. There is no L1/L2 file dedicated to this discipline — the entity records (owned by the relational awareness layer) carry the per-entity state.

3. **Add the constitutional section to `RULES.md`.** Paste the snippet under your next free `§<N>`. The framework will not mutate your constitution unprompted.

4. **Make sure entity records have honest `confidence_basis` values.** The discipline reads `knowledge_state`, `classification_confidence`, and `confidence_basis` directly from `memory/L2/entities/<slug>.md`. Empty or generic `confidence_basis` strings undermine the discipline. The doctor check (Phase 5) flags coverage below 80%.

5. **Restart the gateway.**

   ```bash
   jc gateway restart
   ```

6. **Verify.** Run `jc-doctor` and look for the "Adaptive Discovery" section (Phase 5).

## How the stakes threshold works

The acceptable inference threshold scales with consequences:

| Stakes | Inference acceptable?        | Action                                                                |
|--------|------------------------------|-----------------------------------------------------------------------|
| Low    | Yes, even with `low` confidence | Proceed; document the inference; correct if proven wrong.          |
| Medium | Only with `medium`+ confidence | Ask the entity or a knowledgeable peer to confirm; then proceed.   |
| High   | Never on inference alone       | Escalate to the human authority; pause until cleared.              |

"Stakes" is the agent's judgment, considering: reversibility, public visibility, financial impact, reputational impact, second-order effects. The discipline does not encode a financial threshold — operators with concrete heuristics can extend it (see spec Open questions).

## The conservative default for unknowns

When the agent meets an entity that does not appear in `entities/` (or appears as `unknown / inferred / low`), the default posture is:

- Formal register.
- No commitments on behalf of the principal.
- Passive observation: log, do not act.
- The unknown stays unknown until evidence resolves it. The agent does NOT fill the gap by inventing a plausible role.

This is the most important behavioral floor the discipline introduces. It is also the rule most often tested in practice — an unfamiliar inbound that "feels" like a known category invites premature classification.

## Gotchas

- **Don't conflate declared and inferred.** Inferences and facts in the same sentence without explicit marking is the failure mode. Compare:
  - Wrong: "Diego is the procurement lead at Zenita."
  - Right: "Diego is the procurement lead at Zenita [declared, principal 2026-04-21]. He probably handles vendor negotiations end-to-end [inferred, sign-off pattern across three threads]."
- **Don't promote `unknown` on a hunch.** The promotion path is `unknown / inferred / low` → observe → propose → enact via the accountabilities authority flow. Skipping straight to `declared` because "it's obvious" is the discipline failing.
- **`high_stakes_escalation_channel` must be reachable.** If misconfigured, high-stakes actions may escalate to a channel that is no longer monitored. The doctor check validates reachability: if `authority`, then `accountabilities.enabled` must be `true` and `authority_channel` not `none`; if an explicit channel slug, it must match a channel in `channels:`.
- **`confidence_basis` is freeform; keep it factual.** Operators must avoid documenting suspicions or judgments. The field describes *why this classification is justified*, not opinions about the entity's character.
- **Mutual self-disclosure is reciprocal.** When the agent meets a peer for the first time, it introduces itself and expects a symmetrical response. A peer that refuses to disclose is `unknown` until the principal resolves the ambiguity. Asymmetric introductions are a smell.
- **This section does not override the Accountability Principle.** Perimeter classification (Inside / Adjacent / Outside / Delegated) still runs first. Adaptive Discovery governs *how* the agent reasons about the requester; the accountability flow governs *whether* the request is in scope.

## Where to go deeper

- Full design + phases: [`docs/specs/adaptive-discovery.md`](./specs/adaptive-discovery.md).
- Template: `templates/instance/memory/L1/RULES.md.adaptive-discovery-section.template`.
- Related: [`docs/entities.md`](./entities.md), [`docs/inter-agent-protocol.md`](./inter-agent-protocol.md).
