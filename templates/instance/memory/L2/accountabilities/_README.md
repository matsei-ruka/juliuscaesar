---
slug: accountabilities-readme
title: Accountabilities — L2 directory README
layer: L2
type: framework-readme
state: active
tags: [accountability, framework, readme]
---

# Accountabilities — L2 directory

## What this directory is for

This directory holds one **detail file per accountability** declared in `memory/L1/accountabilities-manifest.md`. The L1 manifest carries the roster (name + default level + link); each L2 file expands a single roster row into the structured detail the agent needs to classify and act.

The agent reads these files when classifying inbound requests. Heterogeneous detail files break the classification flow — every detail file MUST use the same 9-section structure described below.

## The 9-section structure

Every accountability detail file MUST contain all nine of the following sections, in this order:

1. **Scope** — what the accountability covers; what counts as "Inside".
2. **Out of scope (perimeter — explicit)** — the boundary, named. Concrete examples of requests that *look* related but are NOT this accountability.
3. **Outputs** — the artifacts, decisions, or communications the agent produces under this accountability.
4. **Stakeholders** — who is involved; who the agent talks to; who owns what.
5. **Cadence** — how often the work runs (ad-hoc, weekly, on-trigger), and any timing constraints.
6. **Decision boundary** — what the agent can decide alone vs. what requires the primary operator or another party.
7. **Adjacency notes** — when the default level shifts (e.g., "Inside by default, but Adjacent for any commitment > €X"). These notes are how the agent moves between levels for the same accountability.
8. **Self-check pre-action** — the literal questions the agent asks itself before acting under this accountability. Used at every engagement.
9. **Connections to existing constitution** — pointers to relevant `RULES.md` / `RULES_TECH.md` sections that govern HOW the agent operates within this accountability.

## Why all 9 sections MUST be present

The agent's classification flow expects this structure. When the agent runs its self-check on an inbound request, it reads section 8 (Self-check pre-action) directly from the detail file. When it weighs whether the default level should shift, it reads section 7 (Adjacency notes). Missing sections break that flow — the agent ends up improvising, which is the failure mode the manifest exists to prevent.

Empty sections are acceptable while drafting (`…` is fine as a placeholder). Missing sections are not.

## Example slugs

Slugs are kebab-case, descriptive, concise. Examples:

- `financial-reporting`
- `vendor-negotiation`
- `team-coordination`
- `technical-architecture`
- `hiring-decisions`
- `client-communication`
- `external-comms`
- `product-roadmap`

## Naming convention

- kebab-case (`vendor-negotiation`, not `VendorNegotiation` or `vendor_negotiation`)
- descriptive — a reader should guess the scope from the slug
- concise — 1–3 words; avoid full sentences
- stable — the slug is referenced from L1 and from `_audit.md`, so renames cost trail
- avoid leading underscore — `_README.md` and `_audit.md` are reserved framework files (see below)

## Starting from the template

Copy `<slug>.md.template` to `<your-slug>.md` and fill in each section. Keep the YAML frontmatter intact; update `slug`, `title`, `default_level`, `created`, `updated`, and `tags`.

## Reserved files

`_README.md` (this file) and `_audit.md` (the enactment audit log, written by the agent — see `docs/specs/accountabilities.md` §Phase 4) are framework files. Do not create accountabilities named `_readme` or `_audit`.
