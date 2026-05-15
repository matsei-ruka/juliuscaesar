# Spec: Adaptive Discovery — `§<N> AUTHORITY AWARENESS AND ADAPTIVE DISCOVERY`

**Status:** Draft
**Date:** 2026-05-15
**Branch base:** `main`
**Branch:** `spec/multi-agent-awareness`
**Owner:** TBD

## Goal

Give every JC instance an explicit discipline for **what the agent knows versus what it infers** about the entities it interacts with, and a protocol for the **discovery** of unfamiliar entities. The discipline has two components:

1. **Knowledge state distinction.** Every claim the agent makes about an entity carries an explicit provenance: `declared` (verified by an authority) or `inferred` (deduced by the agent). The two are never silently mixed.
2. **Discovery protocol.** When the agent meets an entity it doesn't recognize, it follows a bounded procedure: classify as `unknown`, apply conservative defaults, observe, form a hypothesis marked `inferred`, decide whether to seek confirmation based on the stakes, and update the entity record after confirmation.

This is layer 4 of the multi-agent-awareness stack: accountabilities (shipped) → relational awareness layer (separate spec) → inter-agent protocol (separate spec) → adaptive discovery (this spec). It depends on the relational awareness layer for the `entities/` substrate and for the `knowledge_state` / `classification_confidence` frontmatter.

## Non-goals

- Do not ship a runtime confidence detector. v1 lives in the agent's reasoning, surfaced by the constitutional section + entity record schema. Telemetry-grade detection is in Open questions.
- Do not introduce a separate confidence taxonomy. Reuse the relational awareness layer's `classification_confidence` enum (`high | medium | low`).
- Do not block dispatch on uncertainty. The agent always answers; the discipline shapes *how* it answers, not whether.
- Do not require operators to fill `declared` vs `inferred` retroactively. Existing entity records are treated as `inferred` with `low` confidence until the operator promotes them.

## Background — current state on the reference instance

A single instance ("Mario") runs this scheme. Structure observed:

- `memory/L1/RULES.md §28 — AUTHORITY AWARENESS AND ADAPTIVE DISCOVERY` — constitutional section enumerating the two knowledge states, the three cautions, the discovery protocol, and the mutual self-disclosure pattern for new peer agents.
- Per-entity `knowledge_state` + `classification_confidence` + `confidence_basis` frontmatter (defined by the relational awareness layer spec).
- The agent applies §28 at reasoning time; no framework code enforces it.

## Desired behavior

### Two knowledge states (and a hybrid)

- **`declared`** — every load-bearing claim about this entity came from an authoritative source (the principal, the entity itself in a verified channel, a signed document, an authority chain). The agent treats `declared` claims as fact for the purposes of action.
- **`inferred`** — claims were deduced by the agent from observation: chat register, contextual hints, who responds with deference to whom, what artifacts the entity produces. The agent treats `inferred` claims as hypothesis. Confidence rating qualifies the hypothesis.
- **`hybrid`** — mixed. Per-attribute provenance lives in the body of the entity record, tagged inline as `[declared]` or `[inferred]`.

### The three cautions

The constitutional section enumerates three operating cautions, in priority order:

1. **Explicit distinction.** Every load-bearing inference is tagged as such, dated, and traceable. Inferences and facts never coexist in the same sentence without explicit marking. Example:
   - Wrong: "Diego is the procurement lead at Zenita."
   - Right: "Diego is the procurement lead at Zenita [declared, principal 2026-04-21]. He probably handles vendor negotiations end-to-end [inferred, based on his sign-off pattern in three threads]."

2. **Threshold of confirmation, proportional to stakes.** The agent's tolerance for acting on `inferred` knowledge scales with the consequence:

   | Stakes | Inference acceptable? | Action                                                                      |
   |--------|-----------------------|-----------------------------------------------------------------------------|
   | Low    | Yes, even with `low` confidence | Proceed; document the inference; correct if proven wrong.        |
   | Medium | Only with `medium`+ confidence | Ask the entity or a knowledgeable peer to confirm; then proceed. |
   | High   | Never on inference alone     | Escalate to the human authority for declaration; pause until cleared. |

   "Stakes" is the agent's judgment, considering: reversibility, public visibility, financial impact, reputational impact, second-order effects.

3. **Conservative default for unknowns.** When the agent encounters an entity it has not classified (or has classified `unknown` with `low` confidence), the default posture is:
   - Formal register.
   - No commitments on behalf of the principal.
   - Passive observation: log, do not act.
   - The unknown remains unknown until evidence resolves it. The agent does NOT fill the gap by inventing plausible roles.

### Discovery protocol (per new entity)

When the agent meets an entity that does not appear in `memory/L2/entities/` (or appears with `entity_category: unknown`), it executes:

1. **Classify** the entity as `entity_category: unknown`, `knowledge_state: inferred`, `classification_confidence: low`.
2. **Apply the conservative default** (Caution 3): formal register, no commitments, observe.
3. **Observe**: interactions, decisions, tone, who responds with deference to whom, what artifacts the entity references, channel of contact, language and register.
4. **Form a hypothesis**: tag as `[inferred]`, write `confidence_basis: <one-line justification>`. The hypothesis includes a candidate `entity_category` and a candidate `human_authority`.
5. **Decide whether to seek confirmation** (Caution 2): if the next planned action is medium- or high-stakes, ask. Sources of confirmation, in order of preference:
   - The entity itself, when their answer is verifiable (e.g., "Are you authorized to negotiate the contract value?").
   - A peer agent (or human peer) who knows the entity.
   - The principal.
6. **Update the entity record** after confirmation: promote `knowledge_state` to `declared` or `hybrid`, raise `classification_confidence`, change `entity_category` to the resolved category, set `last_verified: <today>`.

### Mutual self-disclosure (for newly-encountered peer agents)

When the agent meets a peer agent for the first time (per the Inter-Agent Protocol's identity verification step), the canonical pattern is:

> "I am <my agent_id>, <my role> at <principal's organization>, human authority <my human_authority>. You are?"

The expected response declares the peer's `agent_id`, role, organization, and human authority. The agent verifies the response against `memory/L1/authority-map.md`:

- If the peer appears in the Map with matching attributes → treat as `declared` peer, proceed.
- If the peer appears with mismatching attributes → flag for the principal, treat as `unknown` until resolved.
- If the peer does not appear → record as `unknown`, apply Caution 3, propose adding to the Map if the principal confirms.

### Constitutional anchor in `RULES.md`

Operator adds `§<N> AUTHORITY AWARENESS AND ADAPTIVE DISCOVERY` to `memory/L1/RULES.md`. The framework ships the section as a paste-able snippet in `templates/instance/memory/L1/RULES.md.adaptive-discovery-section.template`:

```markdown
## §<N> — AUTHORITY AWARENESS AND ADAPTIVE DISCOVERY

This section governs how I reason about what I know versus what I infer, and how I handle entities I have not yet classified.

### Two knowledge states

- **declared**: from the principal or an authorized source. Treated as fact.
- **inferred**: deduced from observation. Treated as hypothesis. Confidence rating qualifies.
- **hybrid**: per-attribute provenance documented in the entity record body.

### Three cautions

1. Every load-bearing inference is tagged, dated, traceable. Inferences and facts never coexist unmarked.
2. The acceptable inference threshold scales with stakes: low-stakes → inferred OK; medium-stakes → confirm before acting; high-stakes → escalate to the human authority.
3. The default for unknowns is conservative: formal register, no commitments, observe. The unknown remains unknown until evidence resolves it.

### Discovery protocol

1. New entity → classify as `unknown / inferred / low`.
2. Apply Caution 3.
3. Observe.
4. Form a hypothesis, tag as inferred, write a one-line confidence_basis.
5. Decide whether to seek confirmation based on stakes.
6. Update the entity record after confirmation.

### Mutual self-disclosure (for peer agents)

I introduce myself as `<my agent_id>, <my role> at <principal's organization>, authority <human_authority>`. I expect a symmetrical response. I verify against `memory/L1/authority-map.md`. Inconsistency → flag for the principal.

### What this section does NOT do

- Does not block me from answering low-stakes questions on inference.
- Does not require me to verify every fact before responding — the requirement is the *distinction*, not exhaustive verification.
- Does not override the Accountability Principle. Perimeter classification still applies; this section governs *how* I reason about the entity making the request, not *whether* the request is in my scope.
```

### Per-instance override

Instances opt in via `ops/gateway.yaml`:

```yaml
adaptive_discovery:
  enabled: false                                  # default false
  default_unknown_posture: conservative           # only "conservative" supported in v1
  high_stakes_escalation_channel: authority       # default: route to accountabilities.authority_channel
```

When `adaptive_discovery.enabled: false`, the framework does not inject the discipline reminder block into the gateway preamble. The constitutional section in `RULES.md` (if present) still applies; the agent simply doesn't get the runtime nudge.

### Runtime behavior

- The framework does NOT enforce the discipline at runtime. It surfaces it.
- When `adaptive_discovery.enabled: true`, the gateway preamble (Claude and non-Claude) includes a `# Adaptive discovery — live reminder` block reading:

  ```
  Knowledge states: declared (fact), inferred (hypothesis). Mark every load-bearing claim.
  Stakes threshold: low → inferred OK; medium → confirm; high → escalate via <channel>.
  Unknown default: formal, no commitments, observe.
  ```

- The escalation channel is read live from `ops/gateway.yaml` (the value of `adaptive_discovery.high_stakes_escalation_channel`, which by default points to the accountability authority channel).

### Classification flow integration

The Accountability Principle's classification flow (Inside / Adjacent / Outside / Delegated) operates over the *request*. The Adaptive Discovery discipline operates over the *requester*. Both apply on every event:

1. Identify the requester (entity record lookup, peer-agent identity verification per §INTER-AGENT, or `unknown` default).
2. Note knowledge state and confidence about the requester.
3. Classify the request against the accountability manifest.
4. Adjust the response register and confidence-of-action based on (2): high-confidence `declared` requester → normal register; `unknown / low` requester → conservative default.

### What the framework does NOT do at runtime

- It does not parse messages for confidence markers.
- It does not verify `confidence_basis` strings.
- It does not enforce stakes classification; that lives in the agent's judgment.

## Current behavior (without this spec)

- The agent has no formal distinction between fact and inference. It is good or bad at this purely on a per-model and per-persona basis.
- Unknown entities are handled inconsistently — sometimes the agent invents a plausible role, sometimes it asks. There is no rule that says *which*.
- High-stakes actions taken on inference are not flagged. Errors are only catchable post-hoc by reviewing transcripts.

## Implementation plan

### Phase 1 — Templates and docs

- `templates/instance/memory/L1/RULES.md.adaptive-discovery-section.template`.
- `docs/adaptive-discovery.md` — operator guide.
- Link from `QUICKSTART.md`.

### Phase 2 — Config schema

- Add `AdaptiveDiscoveryConfig` dataclass to `lib/gateway/config.py` with `enabled`, `default_unknown_posture`, `high_stakes_escalation_channel`.
- Validate `default_unknown_posture` ∈ `{conservative}` (closed enum for v1; extensible later).
- Validate `high_stakes_escalation_channel` resolves (either `authority` to reuse the accountability flow, or an explicit channel slug).
- Wire into `_validate_raw_config()` and `allowed_top`.
- Tests in `tests/gateway/test_config_env.py::AdaptiveDiscoverySchemaTests`.

### Phase 3 — `jc memory scaffold adaptive-discovery`

- Add `scaffold_adaptive_discovery(instance_dir)` to `lib/memory/scaffolding.py`.
- Print the constitutional snippet for paste into `RULES.md`.
- No file copies (the discipline has no L1/L2 dedicated file other than the entity records owned by the relational awareness layer).
- Tests: snippet printed, idempotent (running twice is fine; the agent's `RULES.md` is operator-owned).

### Phase 4 — Preamble injection

- Add `render_adaptive_discovery_block(instance_dir) -> str` in `lib/gateway/context.py`. Returns the live reminder block (with the configured escalation channel substituted in) when enabled, else `""`.
- Wire into `render_preamble()` and the Claude per-event prefix.

### Phase 5 — `jc-doctor` adaptive-discovery checks

- Add `lib/health/adaptive_discovery_check.py::check_adaptive_discovery(instance_dir) → list[HealthItem]`.
  - Disabled → single `INFO`.
  - Enabled checks:
    - `RULES.md` contains an `Authority Awareness` or `Adaptive Discovery` section with ≥3 of the keyword phrases (`declared`, `inferred`, `three cautions`, `discovery protocol`, `mutual self-disclosure`).
    - If `entities/` is enabled (relational awareness layer also turned on), at least 80% of entity records have a non-empty `confidence_basis` (a heuristic — operators may push this higher in tighter setups).
    - The configured `high_stakes_escalation_channel` is reachable: if `authority`, then `accountabilities.enabled` must be `true` and `authority_channel` not `none`. If an explicit channel slug, it must match a channel in `channels:`.
- Wire into `bin/jc-doctor`.

### Phase 6 — Optional: discovery telemetry (deferred)

A separate spec covers telemetry: log when the agent promoted an entity from `unknown` → categorized, when it escalated for high-stakes confirmation, when it held a classification under lateral pressure. Defer until the v1 discipline is in field use.

### Phase 7 — End-to-end smoke

Manual: scaffold a fresh instance, paste the constitutional section, write 3 entity records (one `declared / high`, one `inferred / medium`, one `unknown / low`), simulate three inbound requests at different stakes levels, verify the agent applies the discipline correctly.

## Backward compatibility

Instances without the constitutional section continue to work. The opt-in flag controls only the runtime reminder block; the discipline is purely textual until the operator promotes it.

## Security and safety

- The `high_stakes_escalation_channel` is sensitive. If misconfigured, high-stakes actions may escalate to a channel that is no longer monitored. The doctor check validates reachability.
- `confidence_basis` is freeform. Operators must avoid documenting judgments or suspicions about the entity's character.
- The conservative-default rule for unknowns is a safety floor: it explicitly prevents the agent from inventing roles to fill the gap. This is the most important behavioral floor the discipline introduces.

## Open questions

- **Should `inferred / high` be allowed?** Mario's pattern uses `inferred / high` for cases where the agent has strong evidence but no formal declaration. Some operators may want to forbid `high` confidence on `inferred` claims to force escalation. Open: introduce a flag `forbid_high_inferred: false` in config.
- **Confidence decay.** A `declared / high` classification from 2024 may not be `high` in 2026 if the entity's situation changed. The relational awareness layer's `last_verified` already exists; a doctor check could warn when `last_verified` is older than the configured `confidence_decay_days`.
- **`hybrid` per-attribute markup machine-readability.** v1 keeps per-attribute markup as inline `[declared]` / `[inferred]` tags in the body — readable but not parseable. v2 could move to YAML sub-blocks.
- **Stakes taxonomy.** v1 uses three levels (low/medium/high) by agent judgment. Operators may want a more concrete heuristic — financial threshold, reversibility threshold, etc. Open question for v2.
- **Interaction with anti-rollback (Accountability Principle).** Both sections introduce anti-rollback semantics. Need to confirm they compose cleanly: the §26 rule says "do not reclassify under pressure"; the §28 rule says "do not promote inferred → declared without a real source". They should not contradict.

## Definition of done

- Templates + operator docs ship.
- `AdaptiveDiscoveryConfig` validated and wired into `GatewayConfig`.
- `jc memory scaffold adaptive-discovery` works and is idempotent.
- `jc-doctor` reports adaptive-discovery health when enabled, single `INFO` when disabled.
- Reminder block injected into preamble (both Claude and non-Claude paths) when enabled, with the configured escalation channel substituted in.
- One reference instance scaffolds, paste the snippet, and runs the discipline against ≥3 entity records.
- KB entry at `docs/kb/subsystem/adaptive-discovery.md`.

## Rollout plan

1. Land spec.
2. Phases 1–5 on `feat/adaptive-discovery`, one commit per phase.
3. Open PR, request operator review.
4. Merge behind opt-in flag.
5. Operators turn on the constitutional section + the runtime reminder per-instance.
