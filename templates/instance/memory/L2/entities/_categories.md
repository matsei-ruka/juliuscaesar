---
slug: entities-categories
title: Entities — six-category reference
layer: L2
type: framework-reference
state: active
tags: [entities, framework, reference]
---

# Entities — the six categories

Closed enum for `entity_category` in v1. Operators may not redefine the set; extensibility is tracked in `docs/specs/relational-awareness-layer.md` Open questions.

One anonymous example per category — meant to anchor the operator's judgment, not to be copy-pasted as real entities.

## `internal_authority`

The principal of this instance, plus anyone explicitly delegated authority over the agent.

> **Example.** A founder/CEO who owns this assistant; or a board member with explicit operating authority over a function the agent runs.

Hallmark: the agent treats their declared instructions as authoritative without further escalation.

## `internal_peer`

Colleagues at the agent's level — human or agent — inside the principal's organization.

> **Example.** A co-founder, head of a sibling function, or another JC instance reporting to the same principal.

Hallmark: peer-to-peer coordination. The agent collaborates, does not defer; conflicts resolve up at the human authority layer (see `docs/inter-agent-protocol.md`).

## `external_client`

Clients the agent serves on behalf of the principal.

> **Example.** A buyer the agent corresponds with on a recurring deal; a tenant for a real-estate-advisor persona; a portfolio company the agent supports.

Hallmark: outbound communication is service-shaped. The agent represents the principal *to* the entity.

## `external_vendor`

Suppliers, partners, and service providers to the principal.

> **Example.** An infrastructure vendor, a law firm on retainer, a payment processor's account manager.

Hallmark: the principal pays the entity (or receives services from them). The agent coordinates execution but does not commit on behalf of the vendor.

## `external_occasional`

One-off external contacts — intros, networking, transient.

> **Example.** An attendee at a one-time event who asked for follow-up; a journalist on a single piece; a referral from a peer who emailed once.

Hallmark: short engagement horizon. No ongoing accountability; record exists so the agent does not re-classify a returning contact as `unknown` next month.

## `unknown`

Entity exists but is not yet classified.

> **Example.** A brand-new inbound from an unfamiliar address; a person mentioned by a client without context; a peer-shaped channel that has not completed mutual self-disclosure.

Hallmark: the agent applies the conservative default (formal register, no commitments on behalf of the principal, passive observation) per `docs/adaptive-discovery.md`. The unknown remains unknown until evidence resolves it. The agent does NOT fill the gap by inventing a plausible category.

## Notes on use

- Move between categories deliberately — every transition is gated by the accountabilities authority flow.
- Most records start `unknown` and graduate. That's the expected path; do not skip the `unknown` step just because the agent has a hypothesis.
- Two categories are *almost* always wrong:
  - `internal_authority` for anyone other than the principal and explicitly-delegated authorities. If you're unsure, it's `internal_peer`.
  - `external_client` for vendors. The direction of service flow distinguishes them.
