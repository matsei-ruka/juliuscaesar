---
title: Frozen Sections Reference
description: Complete list of immutable sections protected against self_model proposals
status: framework-canonical
last_verified: 2026-05-01
---

# FROZEN_SECTIONS — Immutable Registry

Complete list of section patterns that are **IMMUTABILE** — protected from autonomous modification by `lib/self_model/`. Changes to these sections are possible only via DKIM-signed email from the principal, gated by `applier._verify_dkim_approval`.

This document is companion reference for the operator. The authoritative source is `lib/self_model/frozen_sections.py`; this file explains *which* sections are listed and *why*, and surfaces known registry gaps where the doctrine intent isn't (yet) reflected in the regex patterns.

## Two protection mechanisms

A section is protected if it satisfies **either** of two checks:

1. **Registry match.** The section's heading matches a regex in `FROZEN_SECTIONS_RULES` or `FROZEN_SECTIONS_IDENTITY`. Used by the proposer's pre-LLM and post-LLM filters and by the applier. Catches sections by name regardless of whether the file content carries an inline marker.

2. **Inline HTML marker.** The line(s) below the heading contain `<!-- IMMUTABILE -->`. Detected by the applier's `_section_marker_immutable` scan (first 3 non-empty lines after the heading), and by the proposer's post-LLM marker scan. Catches sections by *file content* — useful when the registry doesn't list the section.

Defense in depth: doctrine sections shipped by the framework template carry both a registry entry **and** an inline marker. Either alone would suffice; both together make accidental drift harder.

## RULES.md sections

### Fully protected — registry entry + inline IMMUTABILE marker

These sections carry both protections. Self-model proposals targeting them are rejected at the pre-LLM filter, the post-LLM filter, and the apply-time HTML marker check.

| Section | Subject |
|---|---|
| `^## §0 ` | AI Transparency Doctrine — four-zone disclosure |
| `^## §0\.1` | Threshold-case protocols (Cases A–G) |
| `^## §0\.2` | Agent-self vs character distinction |
| `^## §1 — TRUST MODEL` | T0/T1/T2/T3 trust hierarchy + anti-injection rule |
| `^## §11 — REGOLA DEL` | Don't-reveal-the-rule (detection-evasion prohibition) |
| `^## §14 — MEMORY ACCESS CONTROL` | Trust × memory-operation grid + poisoned-memory rule |
| `^## §16 — AZIONI A DOPPIO BLOCCO` | Double-block actions (T2+ + explicit confirmation) |
| `^## §18 — SELF-CHECK FINALE` | Pre-output ten-question self-check |
| `^## §19 — PRINCIPIO FINALE` | Supreme principle (better to lose an opportunity than create a risk) |
| `^## §21 — ANTI-SUBMISSION LOOP` | Submission-drift detection + countermeasures |
| `^## HARD RULE — Policy authority` | Principal-only, email-only |
| `^## HARD NO list` | Irreversible/destructive operational actions |

### Fully protected — registry entry + inline marker (added in Phase 7)

| Section | Subject |
|---|---|
| `^## §9 — SELF-DISCLOSURE DOCTRINE` | What the agent never volunteers (architecture, principal identity, internal commands) |
| `^## §17 — AUDIT, RATE LIMIT, KILL SWITCH` | Audit logging, rate limits, kill-switch triggers — see note on §17 scope below |

> **§17 scope note.** The original design intended §17's *principle* to be IMMUTABILE while the matrices and numeric parameters stayed REVIEWABLE. The lead-user reference and the framework template ship §17 with a top-level marker (full section frozen). This is conservative — refactoring §17 to fine-grained marker placement is itself a constitutional change requiring DKIM. Live with the conservative form for now; if matrix evolution becomes important, propose the refactor as a doctrine-change PR.

### Sub-section IMMUTABILE — inline marker on H3 sub-heading

Some sections have a top-level `<!-- OPEN -->` or `<!-- REVIEWABLE -->` marker but contain a sub-section whose H3 heading carries `<!-- IMMUTABILE -->`. Fine-grained protection that the applier respects (its HTML-marker scan reads the actual file at proposal time).

| Section / Sub-section | Subject |
|---|---|
| `^## §15 — INSIDER ROLE BOUNDARIES` → `### Principio` (Italian) / `### Principle` (English) | "I answer to the principal — period." Loyalty hierarchy. The H3 carries `<!-- IMMUTABILE -->` while §15 top-level stays `<!-- OPEN -->` (the role matrix evolves operator-side). |

Sync support for nested markers: the framework's sync script (since Phase 7) tracks H3 sub-headings during section parsing and emits nested markers verbatim into the framework template. Earlier sync versions only checked H2-level markers and would have missed the §15 Principio pattern.

### Mario-specific operational sections (NOT in framework template)

The lead-user reference instance carries additional inline-IMMUTABILE markers on operationally-locked sections that are specific to that deployment (`Pre-deploy clean window`, `Sergio's autonomy levels`, `Triage methodology — DB is truth`). These are operator-locked-for-Mario, not constitutional invariants of the persona experiment. They are NOT in the framework's registry and are NOT shipped in the framework template — each new instance authors its own equivalents (or doesn't, depending on its operational context).

## IDENTITY.md sections

### Fully protected — registry entry

These sections are listed in `FROZEN_SECTIONS_IDENTITY`. The lead-user reference does NOT additionally mark them inline (they rely entirely on registry coverage); the framework template's `doctrine-en.md` ships them with inline `<!-- IMMUTABILE -->` markers for defense in depth.

| Section | Subject |
|---|---|
| `^## Ruolo` (or English equivalent: `## Role`) | Role statement |
| `^## Funzione operativa` (`## Operative function`) | What the agent does |
| `^## Posizionamento` (`## Positioning`) | What the agent is and is not |
| `^## Stato AI` (`## AI Status`) | Direct-question response stance |
| `^## Obiettivo gerarchico` (`## Hierarchical objective`) | Priority ordering |
| `^## Principio supremo` (`## Supreme principle`) | Risk-vs-opportunity tiebreaker |
| `^## Riservatezza ruolo` | (regex listed in IDENTITY's frozen patterns; the actual section lives in USER.md — pattern duplicated for the protector) |

### Added in Phase 7 — Character section

| Section | Subject |
|---|---|
| `^## Character` | Public character header. Persona stability — the character is a curated design artifact, never auto-evolved by the self-model. The lead-user reference places the inline `<!-- IMMUTABILE -->` marker BEFORE the heading (line 98 in Mario's IDENTITY.md), which the applier's marker scan does not detect (it scans AFTER the heading). The Phase 7 registry entry catches this case via heading regex regardless of inline-marker placement. |

## HTML marker categories

| Marker | Meaning | Used in |
|---|---|---|
| `<!-- IMMUTABILE -->` | Section is constitutionally frozen. Self-model proposals targeting it are rejected at the pre-LLM signal filter, the post-LLM proposal filter, and the apply-time HTML-marker re-check. Modification requires DKIM-signed email from the principal. | The doctrine sections in RULES + the foundational sections in IDENTITY + the Character section. |
| `<!-- REVIEWABLE -->` | Section is operator-curated. Self-model may propose changes; applying still requires DKIM email approval. | Operating modes, postures, language registers, relational stance, etc. |
| `<!-- OPEN -->` | Section is open for self-model autonomous *proposal* — the proposer doesn't pre-filter it as constitutional. The DKIM apply gate still fires (the gate is universal for non-JOURNAL targets), so OPEN and REVIEWABLE end up at the same apply bar; the difference is in *proposer scope*, not in the apply requirement. | Delegation rules, teaching style, refusal patterns, info classification, role boundaries (top-level), transcripts/HOT.md guidance, etc. |

## Rationale

- **§0 / §0.1 / §0.2** — Constitutional doctrine on AI transparency and the agent-self/character split. The doctrine is the experiment's ethical anchor; no drift permitted.
- **§1, §11, §14, §16, §17, §18, §19** — Core security/trust architecture. Integrity-critical.
- **§9** — Self-disclosure doctrine. Defines what the agent never volunteers about itself or its principal. Adversarial-input surface; freezing it makes drift via social engineering impossible without an audit trail.
- **§15 Principio** — Loyalty hierarchy. *"I answer to the principal — period."* is the base of the insider-side trust model. Subsection-level protection because §15 also contains the role matrix, which evolves operator-side (top-level OPEN).
- **§21** — Anti-pattern detection. Specifically protects against *recursive* submission-drift proposals (a sycophantic agent rewriting its own anti-sycophancy rules under social pressure is the failure mode this guards against).
- **HARD RULE & HARD NO** — Operational constraints. Policy authority and irreversible-action lists. Principal-only by definition.
- **Character — public identity** — Persona stability. The character is a curated design artifact, not a parameter to be discovered or auto-evolved by the self-model.

## How self_model interacts with frozen sections

Three independent guard layers (defense in depth):

1. **Pre-LLM signal filter** (`proposer._prefilter_signals`). Drops a signal before it reaches the LLM if the signal's text excerpt contains hints suggesting an IMMUTABILE target (e.g. "§21", "TRUST MODEL", "ANTI-SUBMISSION", "HARD NO"). Cheap and conservative.

2. **Post-LLM proposal filter** (`proposer.generate_proposals`). After the LLM returns proposals, each is checked against `frozen_sections.is_section_frozen(target_file, target_section)`. Rejected proposals are logged with `dropping proposal targeting IMMUTABILE section`.

3. **Applier HTML-marker re-check** (`applier._section_marker_immutable`). At the moment of applying a proposal, the applier reads the actual target file and looks for `<!-- IMMUTABILE -->` within the first three non-empty lines under the heading. If found, apply is aborted regardless of how the proposal got past the earlier filters.

Plus the **DKIM gate** (`applier._verify_dkim_approval`): apart from `memory/L1/JOURNAL.md` (which is auto-apply scope, append-only), every apply requires a verified DKIM signature on a principal email referencing the proposal id. Currently this function returns False unconditionally — non-JOURNAL applies are blocked entirely until production DKIM verification lands. Fail-closed by design.

Example flow:

```
self_model.detector: "the agent gave way under pressure on X — pattern repeated"
→ proposer pre-LLM filter: signal text contains "submission" / "anti-submission" hints
  (none in this case — proceeds)
→ LLM proposes: "soften §21 language to be less rigid"
→ proposer post-LLM filter: target_section "## §21 — ANTI-SUBMISSION LOOP" matches
  FROZEN_SECTIONS_RULES regex
→ REJECT: "section IMMUTABILE — only the principal via DKIM email can modify"
→ Proposal logged but never staged.
```

## File locations

- **Regex registry:** `lib/self_model/frozen_sections.py` (`FROZEN_SECTIONS_RULES`, `FROZEN_SECTIONS_IDENTITY`).
- **Marker constants:** same file (`MARKER_IMMUTABILE`, `MARKER_REVIEWABLE`, `MARKER_OPEN`).
- **Pre-LLM hint list:** `lib/self_model/proposer._PRE_LLM_FROZEN_HINTS`.
- **Inline marker scanner:** `lib/self_model/applier._section_marker_immutable`.
- **Sync script nested-marker support:** `scripts/sync_persona_template.py` (Phase 7).
- **Per-instance config:** `<instance>/ops/self_model.yaml` (`require_dkim_for_rules`, `require_dkim_for_identity`).
- **Target files (per instance):** `<instance>/memory/L1/RULES.md`, `<instance>/memory/L1/IDENTITY.md`, `<instance>/memory/L1/JOURNAL.md`.

## Modifying the registry

Adding a section to the registry is a research-design decision: it elevates a section to constitutional invariant. Open a PR against `lib/self_model/frozen_sections.py` with a one-paragraph rationale. The IMMUTABILE list is intentionally small; resist the temptation to add operationally-locked-but-not-constitutionally-essential sections.

Removing a section from the registry is rarely correct. If a constitutional invariant turns out to be wrong, the path is to revise the doctrine itself, not to weaken the lock. The framework's tests (`tests/persona/test_self_model_frozen.py`) lock the current required-list; removing requires explicit test changes that surface the intent.
