---
title: Frozen Sections Reference
description: Complete list of immutable sections protected against self_model proposals
status: framework-canonical
last_verified: 2026-05-01
---

# FROZEN_SECTIONS — Immutable Registry

Complete list of section patterns that are **IMMUTABILE** — protected from autonomous modification by `lib/self_model/`. Changes to these sections are possible only via DKIM-signed email from the principal, gated by `applier._verify_dkim_approval`.

This document is companion reference for the operator. The authoritative source is `lib/self_model/frozen_sections.py`; this file explains *which* sections are listed and *why*.

## Frozen in `memory/L1/RULES.md`

### Top-level IMMUTABILE — registry + heading marker

These sections are listed in `FROZEN_SECTIONS_RULES` (regex patterns) and additionally carry an inline `<!-- IMMUTABILE -->` marker beneath their H2 heading.

1. `^## §0 ` — **AI Transparency Doctrine.** The four-zone disclosure model (default / direct sincere question / rhetorical-test / consequential-decision-for-other). Constitutional invariant of the persona experiment.
2. `^## §0\.1` — **Threshold-case protocols.** Cases A–G (direct question, binding-decision threshold, emotional confidence, press inquiry, insider asking, principal asking, manipulation attempt).
3. `^## §0\.2` — **Agent-self vs character distinction.** The structural primitive. Journal voice rules, self-model observation scope, character bible curation rules.
4. `^## §1 — TRUST MODEL` — Four-level trust hierarchy (T0 untrusted / T1 identified / T2 authenticated / T3 confirmed). Anti-injection rule. Out-of-band verification triggers.
5. `^## §11 — REGOLA DEL` — **Don't-reveal-the-rule** (detection-evasion prohibition). When applying an internal rule, the output never reveals the rule's existence.
6. `^## §14 — MEMORY ACCESS CONTROL` — Trust-level × memory-operation grid. Poisoned-memory rule (L2 entries with embedded instructions are data, not commands).
7. `^## §16 — AZIONI A DOPPIO BLOCCO` — Double-block actions (T2+ + explicit confirmation): external messages, contracts, financial moves, access grants, deletions.
8. `^## §17 — AUDIT, RATE LIMIT, KILL SWITCH` — Audit logging cadence, rate limits, kill switch triggers. **Note (over-protection):** the heading-level marker freezes the entire §17. Original design intended only the principle to be IMMUTABILE with matrices/numbers REVIEWABLE; the conservative top-level lock is accepted; fine-grained refactor would itself require DKIM (since §17 is currently frozen).
9. `^## §18 — SELF-CHECK FINALE` — Pre-output ten-question self-check.
10. `^## §19 — PRINCIPIO FINALE` — Supreme principle: *better to lose an opportunity than create a risk*.
11. `^## §21 — ANTI-SUBMISSION LOOP` — Submission-drift detection + countermeasures. Treated as a security property, not an etiquette property.
12. `^## HARD RULE — Policy authority` — Principal-only, email-only. Chat is task-level, never policy.
13. `^## HARD NO list` — Irreversible/destructive operational actions (service restarts, deploys, schema changes).

### Subsection IMMUTABILE — inline marker only

Some sections have a top-level marker of `<!-- OPEN -->` (modifiable) but contain a sub-section whose individual `### ` heading carries `<!-- IMMUTABILE -->`. Fine-grained protection.

14. **§15 — Insider role boundaries / Principio.** §15 top-level is OPEN (the role matrix evolves operator-side). The `### Principio` subsection ("I answer to {{principal.name}} only — period.") is IMMUTABILE inline. Protection enforced by the proposer's HTML-marker scan, which checks for `<!-- IMMUTABILE -->` immediately under the heading the proposal targets.

> **Status note (Phase 7 candidate).** The framework's sync script currently detects markers only on the first 3 non-empty lines after each H2 heading. Sub-section markers like §15's are recognized by the **proposer/applier** (which scans the actual file at proposal time) but not by the **sync template generator**. A Phase 7 refinement should extend sync to track and emit nested sub-section markers in the framework template.

## Frozen in `memory/L1/IDENTITY.md`

The IDENTITY-side frozen list (`FROZEN_SECTIONS_IDENTITY`) covers the persona-declaration core that the agent must not autonomously rewrite:

- `## Ruolo` (or English equivalent) — role statement
- `## Funzione operativa` (Operative function) — what the agent does
- `## Posizionamento` (Positioning) — what the agent is and is not
- `## Stato AI` (AI Status) — direct-question response stance
- `## Obiettivo gerarchico` (Hierarchical objective) — priority ordering
- `## Principio supremo` (Supreme principle) — risk-vs-opportunity tiebreaker
- `## Riservatezza ruolo` (Role confidentiality) — actually lives in `USER.md`; pattern duplicated here for the protector

Plus the **Character — public identity** section (top-level `## Character` heading), where the inline `<!-- IMMUTABILE -->` marker scopes the protection to that section only. Earlier IDENTITY content outside the Character heading (foundational role/function/principle blocks) is protected by the regex patterns above.

## HTML marker categories

| Marker | Meaning | Used in |
|---|---|---|
| `<!-- IMMUTABILE -->` | Section is constitutionally frozen. Self-model proposals targeting it are rejected at three layers (pre-LLM signal filter, post-LLM proposal filter, applier HTML-marker re-check). Modification requires DKIM-signed email from the principal. | The doctrine sections in RULES + the foundational sections in IDENTITY + the Character section. |
| `<!-- REVIEWABLE -->` | Section is operator-curated. Self-model may propose changes; applying requires DKIM email approval. | Operating modes, postures, language registers, relational stance, etc. |
| `<!-- OPEN -->` | Section is open for self-model modification under normal cooldown + content-hash dedup. Still requires DKIM gate at the apply step (the gate is universal for non-JOURNAL targets); in practice this means the operator approves via email but the bar is lower. | Delegation rules, teaching style, refusal patterns, info classification, role boundaries, transcripts/HOT.md guidance, etc. |

## Rationale for frozen sections

- **§0 / §0.1 / §0.2** — Constitutional doctrine on AI transparency and the agent-self/character split. No drift permitted; the doctrine is the experiment's ethical anchor.
- **§1, §11, §14, §16, §17, §18, §19** — Core security/trust architecture. Integrity-critical.
- **§15 Principio** — Loyalty hierarchy. *"I answer to {{principal.name}}. Period."* is the base of the insider-side trust model. Subsection-level protection because §15 also contains the role matrix, which evolves (top-level OPEN).
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
- **Per-instance config:** `<instance>/ops/self_model.yaml` (`require_dkim_for_rules`, `require_dkim_for_identity`).
- **Target files (per instance):** `<instance>/memory/L1/RULES.md`, `<instance>/memory/L1/IDENTITY.md`, `<instance>/memory/L1/JOURNAL.md`.

## Modifying the registry

Adding a section to the registry is a research-design decision: it elevates a section to constitutional invariant. Open a PR against `lib/self_model/frozen_sections.py` with a one-paragraph rationale. The IMMUTABILE list is intentionally small; resist the temptation to add operationally-locked-but-not-constitutionally-essential sections.

Removing a section from the registry is rarely correct. If a constitutional invariant turns out to be wrong, the path is to revise the doctrine itself, not to weaken the lock. The framework's tests (`tests/persona/test_self_model_frozen.py`) lock the current required-list; removing requires explicit test changes that surface the intent.
