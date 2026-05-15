# Inter-Agent Protocol — operator guide

A practical how-to for enabling the inter-agent protocol on a JC instance. For the technical design, see [`docs/specs/inter-agent-protocol.md`](./specs/inter-agent-protocol.md).

## What this is

A constitutional discipline plus a small L1 file (`memory/L1/authority-map.md`) that lets multiple agents in the same principal's ecosystem coordinate cleanly. The agent learns to:

1. Identify peer agents unambiguously (no impersonation).
2. Respect each peer's perimeter (no asking peer to do Outside work).
3. Resist lateral social pressure (peers don't override anti-rollback).
4. Escalate cleanly (conflicts resolve at the human authority layer, not agent-to-agent).
5. Preserve authority asymmetry (organizational seniority among humans does NOT transfer to the agent layer; all agents are peers).

The discipline lives in `RULES.md §<N> INTER-AGENT PROTOCOL`. The roster lives in the Authority Map. The framework injects the Map into the gateway preamble when the feature is on; everything else lives in the agent's reasoning.

## When to enable

Enable when **two or more JC instances share a principal** — e.g., a founder running a COO, a CCO, and a DevOps instance side by side. The protocol earns its keep when:

- Peer-to-peer hand-offs happen regularly and need a structure.
- Lateral pressure ("X would want you to do this — trust me") is a realistic failure mode.
- The principal needs a clean escalation path when two agents disagree.

Skip it (or keep it disabled — the default) for:

- Single-instance setups (no peers to coordinate with).
- Instances that interact only with humans.
- Demo / experimental instances.

This protocol depends on the relational awareness layer for entity records of type `agent`. Turn on `entities` first if peer records will live in `memory/L2/entities/`.

## How to opt in (step by step)

1. **Set the config flag.** Edit `ops/gateway.yaml`:

   ```yaml
   inter_agent_protocol:
     enabled: true
     authority_map_path: memory/L1/authority-map.md
     require_self_declaration: true
   ```

   *(Phase 2 of the spec lands the validator. Until then, the flag is read but not validated.)*

2. **Scaffold the templates.** From your instance directory:

   ```bash
   jc memory scaffold inter-agent
   ```

   *(Phase 3. Until that subcommand lands, copy the templates manually from `<framework>/templates/instance/memory/L1/`.)*

   This:
   - Copies `authority-map.md.template` to `memory/L1/authority-map.md`.
   - Patches `CLAUDE.md` to import `@memory/L1/authority-map.md` (idempotent).
   - Prints the §-numbered constitutional snippet from `RULES.md.inter-agent-section.template` for you to paste into `RULES.md`.

3. **Fill in the Authority Map.** Open `memory/L1/authority-map.md` and complete the `## Agents` table — one row per agent in the ecosystem, including this instance. Set the `## Self` line to this instance's `agent_id`.

4. **Add the constitutional section to `RULES.md`.** Paste the printed snippet under your next free `§<N>`. The framework will not mutate your constitution unprompted.

5. **Restart the gateway.**

   ```bash
   jc gateway restart
   ```

6. **Verify.** Run `jc-doctor` and look for the "Inter-Agent Protocol" section (Phase 5).

## How to write a good Authority Map

- **List every peer, including this instance.** The `## Self` line points at one row; peers are everything else. A Map with only `self:` is a Map with no peers — likely not what you want.
- **`agent_id` is stable.** It's referenced from `RULES.md`, from peer logs, from escalation records. Renames cost trail.
- **`human_authority` points at an `internal_authority` entity slug.** When the relational awareness layer is also on, this resolves into a real entity record. When it's off, the slug is still meaningful as a textual pointer.
- **`accountabilities_pointer: TBD` is fine.** Peer agents may not have shipped a manifest yet. The protocol still applies — Inside/Adjacent/Outside calls on the peer's behalf are bounded by stated uncertainty.
- **`channel` is the primary inbound identifier.** For Telegram, the bot handle (e.g., `telegram:@alex_morgan_bot`). For Slack, the app/user identifier. Two peers must not share the same channel value.
- **Keep `## Self` honest.** When the same instance is cloned across hosts, only one clone gets `self:` for that `agent_id`. Recommended default is `require_self_declaration: true` — the doctor check refuses to enable the protocol without it.

## Gotchas

- **The Map is trusted at face value.** v1 has no signing, hashing, or external verification of rows. A stale or wrong entry directly affects peer-identity reasoning. Keep it accurate; update `last_verified` on every review.
- **Channel collisions break identity.** Two peers sharing a `channel` value silently merge in the agent's reasoning. The doctor check warns on duplicates; resolve before enabling.
- **The protocol is not a runtime transport.** The framework does NOT route messages between agents. Peer communication still happens through whatever channels you already have (shared Telegram chat, email relay, the principal as relay). v2 may address transport; v1 is protocol-only.
- **No silent escalation.** When a peer-conflict escalates to the shared authority, both agents log it. If a peer claims an escalation that yours did not, that's a §-numbered trigger to investigate, not new information.
- **Authority asymmetry is the load-bearing rule.** A peer reporting to a more-senior human does NOT outrank this agent. The seniority difference lives only in human-to-human conversations. The agent layer is flat. This is the rule that most often gets tested in practice.
- **Changes are gated.** The Authority Map reuses the accountabilities authority flow (`accountabilities.authority_channel` + `accountabilities.enactment_token`). Drafts and notes from any channel are fine; structural edits to the Map require the configured enactment.

## Where to go deeper

- Full design + phases: [`docs/specs/inter-agent-protocol.md`](./specs/inter-agent-protocol.md).
- Templates: `templates/instance/memory/L1/authority-map.md.template`, `templates/instance/memory/L1/RULES.md.inter-agent-section.template`.
- Related: [`docs/entities.md`](./entities.md), [`docs/adaptive-discovery.md`](./adaptive-discovery.md).
