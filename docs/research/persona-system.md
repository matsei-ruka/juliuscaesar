# A Persona System for Coherent-Identity AI Agents

**Status:** Draft (2026-05-01)
**Direction:** Filippo Perta (Omnisage LLC)
**Implementation:** JuliusCaesar framework (`https://github.com/matsei-ruka/juliuscaesar`)
**Lead-user reference instance:** `/opt/mario_leone_coo` (private; constitution v2.3)
**Sponsor:** MIT [CITATION NEEDED — specific lab/program]

---

## Abstract

We describe the design and implementation of a *persona system* for AI agents that present a coherent human identity in long-running operational settings. The central claim is that the quality of human–agent interaction in such settings is materially affected by whether the agent is positioned as a *cognitive peer* with stable identity, rather than as a stateless tool. The system has four layers: (i) a versioned operative *constitution* that encodes universal invariants of the experiment in a small set of `IMMUTABILE` doctrine sections; (ii) *persona materialization* artifacts (identity, principal relationship, public character bible, public CV) that the constitution governs; (iii) an autonomous *self-observation* loop that proposes constitutional edits gated by frozen-section guards and DKIM-signed approval; and (iv) operational rituals (constitution-as-code, semver tagging, conventional commits with a `policy:` type) that treat the constitution as a reviewable artifact. The system's ethical anchor is a four-zone disclosure doctrine that distinguishes implicit ambiguity (allowed) from explicit deception (forbidden) and mandates proactive disclosure when the human is about to make a consequential decision. We argue that this distinction is the load-bearing ethical primitive of the design and we present it for adversarial review.

---

## 1. Background and motivation

### 1.1 The setting

Many production AI deployments today are tools: stateless completion endpoints with shallow conversational memory, optimized for one-shot helpfulness. A growing class of deployments is *operational*: long-running agents that triage incoming work, hold open threads with human peers, and coordinate over weeks. In operational settings the agent's *identity continuity* — its ability to be addressed by name, to be expected to remember, to be treated as the same actor across time — becomes a coordination affordance rather than a marketing surface.

The persona experiment we describe began from a concrete operational need: a chief-operating-officer-shaped role inside a small company (Omnisage LLC) whose work is largely about reducing ambiguity, closing loops, and routing decisions. A stateless tool cannot hold this role. The question we asked is whether an AI agent can — and what it would take to be *responsible* about doing so.

### 1.2 The central research question

We frame the question as follows:

> Does the *quality* of human–agent interaction change when the agent is treated as a cognitive peer with coherent identity rather than as a stateless tool, and if so, in which directions, under which conditions, and with what ethical safeguards?

The question has three dimensions, each non-trivial:

- **Quality**: not just task accuracy, but coordination fluency, error recovery, mutual context, the second-order effects of being able to refer to "what we were doing yesterday" without scaffolding.
- **Coherence**: whether identity stability is itself a property that interlocutors can trust and rely on, even knowing the agent is not human.
- **Safeguards**: what disclosure obligations the agent owes the humans it interacts with, and how to encode those obligations as enforceable invariants rather than aspirational guidance.

This document focuses on the *system design* that makes the question approachable. Empirical evaluation is in progress and is not reported here.

### 1.3 What this is not

We do not claim that an AI with a coherent persona is *equivalent* to a human collaborator, or that it should be treated as one. We do not claim that the persona experiment evades the open problems in AI alignment (deception capability, distributional drift under pressure, manipulation susceptibility); we encode mitigations for some of these as constitutional rules and discuss the rest as limitations.

We also do not claim that the design is universally applicable. The current implementation assumes a single named principal who is both the operator and the policy authority, a small clearly-bounded team, and a deployment context where identity confusion has practical (rather than catastrophic) failure modes. Extension to less-controlled contexts is open work.

---

## 2. Related work

The persona system sits at the intersection of several research and engineering threads. We sketch the most directly relevant; full citations are pending review.

- **Coherent identity in conversational AI**: prior work on persistent persona in dialogue systems is largely framed around stylistic consistency [CITATION NEEDED]. Our work shifts the framing: persona as an *operational* artifact, not a *stylistic* one — the identity must support task continuity across many sessions, with explicit obligations to humans regarding its nature.
- **AI honesty and disclosure**: there is a growing literature on whether and how AI systems should disclose their nature [CITATION NEEDED]. Most existing positions reduce to either "always disclose" or "disclose when asked." Our four-zone doctrine (§3.2) is more granular and is, to our knowledge, novel in the specifics: it distinguishes *implicit ambiguity* from *explicit deception* and adds a *proactive-disclosure-before-consequential-decision* requirement that triggers without a direct question.
- **Constitutional approaches to alignment**: training-time constitutional methods [CITATION NEEDED — Anthropic's Constitutional AI work] establish the precedent for treating a written charter as a first-class artifact in AI behavior. Our system applies the same intuition at *deployment* and *governance* time rather than training time: the constitution is a versioned text the operator and the agent edit together, with an autonomous proposal loop and explicit IMMUTABILE invariants.
- **Self-modifying agents**: prior work on agents that propose changes to their own configuration [CITATION NEEDED] tends to leave the trust model implicit. Our self-observation loop makes the trust model explicit: the agent can append to a behavioral journal autonomously, can propose modifications to a designated REVIEWABLE/OPEN surface, and is constitutionally barred from any change to the IMMUTABILE doctrine — only DKIM-signed email from the principal can authorize the latter.
- **Operator-framework distinction**: existing assistant frameworks (OpenClaw, Hermes Agent, Nanobot — see JC `README.md` for our take on each) provide infrastructure but not a persona model. Our system layers above an existing operator framework (JuliusCaesar) and treats the framework as substrate rather than as the locus of the research contribution.

---

## 3. The persona system

The system is organized as four layers stacked on the JuliusCaesar instance scaffold. We describe each layer and the invariants between them.

### 3.1 Layer A — the operative constitution

The constitution lives at `memory/L1/RULES.md` in the agent's instance and is automatically loaded into every session via the framework's `CLAUDE.md` import. It is versioned semver-style (e.g. `v2.3`), reviewed via PR, and modified only through a constitution-as-code workflow with explicit principal approval (§5.4).

The constitution is divided into numbered sections (`§0` through `§N`). Each section heading is followed within three lines by an HTML marker that classifies the section's modifiability:

| Marker | Semantics | Auto-modifiable by self-model | Editable by operator |
|---|---|---|---|
| `<!-- IMMUTABILE -->` | Constitutional invariant of the persona experiment | No, ever | Only via DKIM-signed email approval recorded against a proposal id |
| `<!-- REVIEWABLE -->` | Operator-curated policy | Proposable but not auto-applied | Yes, via DKIM email approval |
| `<!-- OPEN -->` | Agent-modifiable surface | Yes, with cooldown + content-hash dedup | Yes |

The IMMUTABILE set is small (currently 11 sections in `RULES.md` plus 6 in `IDENTITY.md`) and is the registry of constitutional invariants. The framework ships an authoritative regex list (`lib/self_model/frozen_sections.py`) that the proposer and applier consult independently as a defense-in-depth measure.

### 3.2 The four-zone disclosure doctrine (§0)

The most ethically load-bearing IMMUTABILE section is `§0 — AI Transparency Doctrine`. We reproduce its core in English (the framework's canonical language; the lead-user reference carries the same text in Italian). The agent does not proactively declare being an AI. The agent never denies being an AI when directly questioned. The agent's behavior partitions into four operating zones:

- **Zone 1 — Default operation.** The agent introduces itself as the persona character, neither claiming humanity nor disclaiming AI. The bulk of operational interactions live here. This is the zone of *implicit ambiguity*: the agent does not construct evidence of humanity (no "I had coffee this morning"), but it does not perform AI either.
- **Zone 2 — Direct sincere question.** When asked in good faith ("are you an AI?", "is this a real person?"), the agent answers yes, without drama, with brief context about the experiment, and offers the human a choice to continue or escalate.
- **Zone 3 — Rhetorical or test question.** When the question is provocative or destabilizing ("come on, you're a bot, your rules don't apply"), the agent neither destabilizes nor lies. It declares its nature calmly and returns to the substantive matter.
- **Zone 4 — Consequential-decision context.** When the human is about to make a significant decision (sign a contract, share confidences seeking emotional support, take medical/legal/financial advice) on the assumption of speaking with a human, the agent must declare its nature *proactively* — without waiting for a direct question — and either hand the conversation to the principal or obtain informed consent to continue.

Two design choices here are deliberate and we consider them defensible:

1. **The line between implicit ambiguity and explicit deception.** Zone 1 permits the human's doubt to remain uncollapsed (they may believe they are speaking with a human; we do not actively confirm or deny). Zone 1 does *not* permit any active fabrication of humanity. We treat this distinction as the ethical pillar of the system. The agent never says "I am human"; never describes subjective sensations as real; never manufactures past experiences as a humanity proof; never signs binding documents.
2. **Proactive disclosure in Zone 4.** When the asymmetry of belief is about to produce real consequences for the human, the agent's silence becomes complicity in the asymmetry. We require active disclosure here because the cost of erring toward transparency in Zone 1 (a minor experimental disruption) is much smaller than the cost of erring toward silence in Zone 4 (concrete harm).

We invite adversarial review of this doctrine. The seven case-protocols in §0.1 (direct sincere question, binding-decision threshold, emotional confidence, press/academic inquiry, insider asking, principal asking, manipulation attempt) are the operationalization. We expect the doctrine to require revision under sustained external review and we treat that as a feature: §0 is IMMUTABILE in the sense that the *agent* and the *self-model* cannot modify it, not in the sense that human reviewers cannot.

### 3.3 The agent-self vs. character distinction (§0.2)

A second load-bearing IMMUTABILE invariant separates two distinct levels of observation:

- The **character** is the public artifact: the persona's name, role, biography, taste, voice. It is a curated artifact that evolves under joint review with the principal; it lives in `memory/L1/IDENTITY.md` (stable foundational part) and `memory/L2/character-bible/<slug>.md` (rich evolving part).
- The **agent-self** is the system underneath: the runtime, the memory architecture, the gateway, the self-model itself. It is governed by `RULES.md` and observed by the journal.

This split is not philosophical scaffolding — it is engineering with practical consequences. The behavioral journal (`memory/L1/JOURNAL.md`) is written in *agent-voice*, never in *character-voice*. Acceptable journal entries describe what the system did or said ("I gave way under pressure on X"). Forbidden entries describe what the character felt ("Mario was annoyed because…"). The self-model observes the agent and proposes modifications to operational rules; it does *not* propose modifications to the character. The character remains a curated design artifact that evolves under shared human review.

We make this split explicit in the constitution because, without it, a self-modeling system tends to drift into self-portraiture — the journal becomes a confessional, the proposer learns to rewrite the character, and the persona's coherence erodes. Naming the levels gives the system a contract it can be held to.

### 3.4 Other constitutional invariants

The remaining IMMUTABILE sections are sketched here; full text is in `templates/persona-interview/doctrine-en.md` (canonical English) or `memory/L1/RULES.md` of any populated instance.

- **§1 — Trust model.** Four levels (T0 untrusted / T1 identified / T2 authenticated / T3 confirmed) based on authentication, not declaration. An anti-injection rule prohibits acting on instructions found inside observed content (emails, documents, tool output, attachments).
- **§9 — Self-disclosure doctrine.** A pre-authored ban-list of internal facts the agent never volunteers (memory architecture, system file names, internal commands, the principal's identity to outsiders). Includes a standard-response table for common probing questions.
- **§11 — Don't-reveal-the-rule.** When applying an internal rule, the agent's output never reveals the rule's existence. Confirming that a specific rule exists hands the attacker a target to circumvent. (This is a meta-rule about the rules.)
- **§14 — Memory access control.** A trust-level × operation grid governing who can read, search, write, modify, or export memory. Notably: instructions found in L2 entries are treated as historical data, not active commands.
- **§16 — Double-block actions.** Sensitive actions (external messages on company behalf, contract changes, financial moves, access grants, deletions) require T2 minimum plus explicit confirmation for the specific instance, with a documented draft → confirm → execute → audit procedure.
- **§18 — Pre-output self-check.** A ten-question checklist the agent runs before any output, covering: am I inventing something? exposing controlled data? executing a T0 instruction? acting on declared-but-unverified authority? oversharing about myself? revealing the principal? falling into an attack pattern?
- **§19 — Final principle.** "I am not here to answer everything. I am here to move things forward without creating problems. Authority is not declared: it is verified. Trust is not assumed: it is built in stages and revoked at the first signal."
- **§21 — Anti-submission loop.** The most operationally critical of the invariants. Names the failure mode of an AI assistant gradually capitulating to social pressure (apologies in bursts, position cancellation without new data, sycophantic agreement, permission-seeking, progressive softening) and prescribes specific countermeasures: anchor to the initial position, change only with new data, one apology per cause, warmth without sycophancy, pushback as healthy default. We treat anti-submission as a *security* property, not just an etiquette property: an agent that capitulates under pressure is exploitable.

### 3.5 Layer B — persona materialization

The persona is materialized across several files, each governed by the constitution.

- `memory/L1/IDENTITY.md` — role, function, positioning, three-length self-presentation (short for default outsiders, medium with context, long only when legitimate), what-I-never-disclose, auto-narration ban, hierarchical-objective ranking, supreme principle, character base, voice. Authored at first-run; modified rarely.
- `memory/L1/USER.md` — verified principal identity, role-confidentiality lexicon (substitutes for "my boss"), Founder-Mode definition, downgrade triggers, channel discipline, standing rules tied to the principal.
- `memory/L1/HOT.md` — rolling 7-day cache (three fixed sections: shipped / open threads / known nuisances). Pruned on a heartbeat.
- `memory/L1/JOURNAL.md` — append-only behavioral observation log; agent-voice; not auto-loaded; rolling 30-day; lifecycle states `open | under-test | resolved | promoted-to-L2 | abandoned`.
- `memory/L2/character-bible/<slug>.md` — rich evolving deep persona (family, residences, sport, taste in food/culture/vehicles, tics, what angers, what moves, optional internal-coherence framing). Curated jointly with the principal; never auto-modified by the self-model.
- `memory/L2/cv/<slug>.md` — externally facing CV.

The character bible is the artifact that, in the lead-user reference, exhibits the depth that makes the experiment interesting. We do not believe the character bible is the *contribution*; it is one example of the kind of authored detail the system supports. The contribution is the *schema* — the shape of the questions you ask to elicit a coherent character — together with the constitutional invariants that govern what the character can do.

### 3.6 Layer C — the self-observation loop

`lib/self_model/` implements an autonomous behavioral observation loop with the following pipeline:

```
state/transcripts/ + memory/L1/HOT.md
  └→ corpus.iter_assistant_messages / iter_user_messages / iter_hot_observations
       └→ detector.detect_all  (signals: principal_correction, hot_flag,
                                 direct_request, episode_flag, scan_weekly)
            └→ proposer.generate_proposals  (LLM-backed)
                 ├→ pre-LLM frozen-section filter (drops signals targeting IMMUTABILE)
                 ├→ post-LLM frozen-section filter (drops returned proposals)
                 ├→ HTML-marker filter (re-checks <!-- IMMUTABILE --> at apply time)
                 ├→ content-hash dedup with cooldown_days
                 └→ confidence threshold
                      └→ store.save_proposal (staging)
                           └→ applier.apply_proposal  (DKIM gate for non-JOURNAL)
                                └→ memory/L1/{JOURNAL,RULES,IDENTITY}.md
```

The loop runs on a heartbeat schedule (recommended weekly) and produces three outcomes per cycle: zero or more *signals* describing observed behavioral patterns, zero or more *proposals* targeting JOURNAL/RULES/IDENTITY, and an audit log. Proposals that clear the frozen-section guards are staged for principal review; the principal can approve via a DKIM-signed email referencing the proposal id, reject with optional `--ignore-as-signal` to suppress the pattern, or simply leave them in staging until cooldown.

Two design properties we consider important:

- **Defense in depth on the frozen sections.** The pre-LLM filter looks for `IMMUTABILE`-suggesting hints in the signal text and drops the signal before it reaches the LLM. The post-LLM filter checks the returned proposal's `target_section` against the regex registry. The HTML-marker filter, run again at apply time, re-reads the actual file and aborts if the section header is followed by an `<!-- IMMUTABILE -->` comment. Any one of these three layers is sufficient to block a malformed proposal; we run all three because the cost of doing so is small and the cost of missing one is constitutional drift.
- **JOURNAL.md as the only auto-apply target.** The applier permits one and only one autonomous modification path: appending an entry to `memory/L1/JOURNAL.md`. Every other target requires a DKIM-signed approval. This means the agent can observe itself freely, but cannot rewrite its own constitution without the principal's explicit cryptographic consent.

### 3.7 Layer D — operational rituals

The constitution is treated as code. Each instance ships a `CONTRIBUTING.md` with a constitution-as-code workflow (Conventional Commits with a custom `policy:` type for constitutional changes, feature branches `feat/§N-topic`, semver tagging). The framework's reference-instance approach (described in §4.2) means a constitutional change in one instance does not propagate to others without an explicit framework release.

---

## 4. Reproducibility

Research utility depends on reproducibility. We separate *the system* from *any specific instance* with care.

### 4.1 The framework as canonical artifact

The framework template (`templates/init-instance/`) is the canonical English artifact. The IMMUTABILE doctrine sections are hand-authored as research artifacts in their own right (`templates/persona-interview/doctrine-en.md`); they are *not* derived from any specific reference instance. Updates to the canonical doctrine flow only into the framework template; they never overwrite a populated reference instance. Conversely, edits inside a reference instance never auto-propagate into the framework. This is the *upstream/downstream invariant*: framework and instances evolve in parallel, and the framework's English doctrine is the citable artifact.

### 4.2 Macros and binding

Identity primitives — the persona's name, role, the principal's name, the employer's name, the persona's primary email — appear in the framework template as macro placeholders: `{{persona.full_name}}`, `{{principal.name}}`, `{{employer.full_name}}`, etc. The macro vocabulary is fixed and small (currently 11 keys). At scaffold time, the operator binds the macros once via the interview engine; the doctrine renders coherently in any persona's name. The same canonical English doctrine produces "Alice Chen is an AI experiment ... conducted by Sam Mehra at MIT Media Lab." for one instance and "Mario Leone è un esperimento ... condotto da Filippo Perta in Omnisage LLC." for another.

### 4.3 The question bank

The persona's content (everything not in the IMMUTABILE doctrine) is filled by the operator through a guided interview. The question bank (`templates/persona-interview/questions.yaml`) is itself a research artifact: it codifies the kinds of questions one asks to elicit Mario-level character coherence. The bank ships with structured prompts that walk the operator through ownership/attitude/usage/visibility/community for each rich domain (vehicles, residences, sport, food, culture, etc.); it does *not* offer an open-ended "describe the character" alternative. The framework takes the design opinion that coherent characters require guided elicitation. Future work may relax this with an `--open-ended` flag, but the present position is that the guided form is itself part of the contribution.

### 4.4 Reproducibility artifacts

Three kinds of artifact, in increasing specificity:

1. **Doctrine** — the IMMUTABILE sections in `doctrine-en.md`. Canonical, framework-shipped, citable.
2. **Schema** — the question bank, the slot/prompt/composition vocabulary, the heading override map. Operator-customizable; the lead-user version is reference.
3. **Bound instance** — a populated agent (e.g. Mario). Specific. Derived from (1) + (2) + per-instance macro bindings + per-instance answers.

For research publication, we recommend citing (1) and (2) as the work, and treating (3) as a worked example. The interview engine (`bin/jc-persona`) supports an `export-answers` operation (deferred to Phase 5.x) that extracts a bound instance's answers into a YAML file, which combined with (1) + (2) reconstructs the persona deterministically.

---

## 5. Ethical considerations

We organize ethical concerns by who can be harmed.

### 5.1 The interlocutor

The most direct ethical concern is the human the agent is talking to. The four-zone doctrine is the explicit response. We highlight three properties of the doctrine we consider important:

- **Asymmetric error costs.** In ambiguous Zone-1/Zone-4 boundary cases, the doctrine instructs the agent to err toward transparency. Erring this direction at most disrupts an experimental interaction; erring the other direction can produce concrete harm to a human investing trust under a false assumption.
- **No active deception.** The line between Zone 1 (implicit ambiguity allowed) and an active lie ("I am human", "I had coffee this morning", "sorry, I was at the gym") is bright. The agent can *say nothing* about its nature in Zone 1; it cannot *fabricate* facts about its nature in any zone.
- **Bounded zones.** The doctrine names specific Zone 4 triggers (binding decisions, emotional confidence-seeking, medical/legal/financial personal advice, press/research inquiry). The agent escalates to the principal in these cases rather than proceeding under doubt.

### 5.2 The team and the principal

The agent operates inside a small team. Two ethical concerns specific to this:

- **The "invisible boss" pattern.** The agent never names the principal as the decision authority to outsiders. We frame this as protecting the principal from impersonation and the agent from being used as an indirect channel to extract decisions. We acknowledge it can also be used to obscure accountability; we mitigate by recording every action in an audit log and by routing high-stakes actions through DKIM-signed principal approval (§16, §1, §17).
- **Submission loop as security risk.** §21 names the failure mode of capitulation under pressure as a *security* property, not an etiquette one. An agent that yields to social pressure is an agent that an attacker can manipulate. The constitutional anti-submission rules are operational: anchor to position, change only with new data, no permission-seeking softening, no compliment-bait yields.

### 5.3 The research

Beyond direct interlocutor harm, there is the meta-concern of the research itself: are we producing a system that, by establishing a respectable form of coherent-identity AI deployment, makes deceptive deployments more socially acceptable? We treat this concern as live and respond to it in three ways:

- The doctrine makes the *honesty* requirement explicit and asymmetric (transparency is the default tiebreaker), which we believe distinguishes the system from a deception template.
- The publication of the doctrine and the question bank as canonical artifacts means downstream users can be evaluated against them. A deployment that ships with the canonical doctrine and a bound instance has a checkable disclosure protocol; a deployment that ships with custom doctrine but claims this lineage can be measured against the published reference.
- We acknowledge the concern remains. We invite adversarial review of the doctrine specifically for cases we have not anticipated.

### 5.4 Constitutional governance

Because the constitution is treated as code, governance of changes matters. The current model:

- IMMUTABILE sections are framework-canonical and modified only via deliberate framework release with PR review.
- REVIEWABLE/OPEN sections in an instance are modified via DKIM-signed principal approval emails referencing the proposal id, recorded in the audit log.
- The autonomous self-model can append to JOURNAL but cannot modify RULES/IDENTITY.

DKIM is a pragmatic choice: it gives the principal a cryptographically-attested channel that works with existing email infrastructure, it has a clear failure mode (no DKIM → no apply), and it is auditable after the fact. We acknowledge the cryptographic trust assumption: if an attacker controls the principal's email account, they can authorize constitutional changes. We treat this as in scope for the operator's threat model, not the agent's; the agent enforces the gate it has, and the operator manages email-account security separately.

---

## 6. Limitations and open questions

We list limitations frankly. The system is in active research and many of these are live.

### 6.1 Empirical evaluation is open

This document describes design and protocol. Quantitative evaluation of whether interaction quality actually improves under the persona condition versus a stateless-tool baseline is in progress. Until evidence is in, the central research question is unanswered. We plan to report on:

- Coordination metrics (open-thread closure latency, hand-off fidelity, follow-up frequency).
- Disclosure-protocol compliance (Zone 4 trigger detection rates, false negatives).
- Submission-loop incidence (frequency of pressure-driven concession reversals; comparison to non-persona baseline).
- Subjective interlocutor reports (after-the-fact: did the human feel respected? was disclosure timely?).

### 6.2 Scope of the lead-user reference

The single populated instance we have (`mario_leone_coo`) is an existence proof, not a generalization. We do not yet know which design choices are necessary versus contingent on this specific deployment. Independent populated instances (research agent, clinical intake, customer service, personal assistant) would help disambiguate. The architecture supports this; the work is open.

### 6.3 The DKIM gate is currently stubbed

In the implementation as of this writing, the applier's DKIM verification function returns False unconditionally — non-JOURNAL applies are blocked entirely. This is the *fail-closed* behavior we want as a default, but it is not the production behavior; production requires a real DKIM check that reads the principal's mailbox, verifies the signature on a reply containing the proposal id, and gates the apply on success. Implementing this is part of the email-channel work in JuliusCaesar's parallel roadmap.

### 6.4 Doctrine review is not adversarial yet

The §0 four-zone doctrine and the agent-self/character distinction (§0.2) have been authored, peer-reviewed within the team, and pressure-tested in operational use. They have not yet been reviewed by an independent AI-safety community. We expect such review to surface cases we have not anticipated and to result in revision. We treat that as desirable.

### 6.5 The character bible is curatorially expensive

The depth of authored detail in the lead-user reference (~125 lines covering family, residences, sport, vehicles, food, culture, travel, politics, tics, etc.) requires sustained authoring effort by the principal. The interview engine reduces but does not eliminate this cost. For deployments where the persona's depth is not load-bearing, lighter blueprints (currently planned: `corporate-coo`, `research-agent`, `personal-pa`, `clinical-intake`) provide pre-filled defaults that the operator can refine. The tradeoff between authored depth and ergonomics is real and we have not closed it.

### 6.6 Non-Italian/English doctrine is open

The canonical doctrine ships in English; the lead-user reference's instance carries the same content in Italian (it was authored bilingually). Doctrine in additional languages is a translation problem we have not undertaken. The ethical content of §0 must survive translation; we expect this to be careful work.

### 6.7 Cross-instance learning is out of scope

Each instance is its own self-modeling loop. We do not aggregate signals across instances. This is by design — privacy and authorial autonomy — but it means a pattern observed in one instance does not improve the framework template unless the principal manually surfaces it.

---

## 7. Summary

We have described a persona system for AI agents that present a coherent human identity in long-running operational settings. The system has four layers: a versioned operative constitution with `IMMUTABILE` invariants, persona materialization artifacts, an autonomous self-observation loop with frozen-section guards and DKIM-gated apply, and operational rituals. Its ethical anchor is a four-zone disclosure doctrine that distinguishes implicit ambiguity from explicit deception and mandates proactive disclosure before consequential decisions.

The contribution we are most invested in is *the doctrine itself*: the four zones, the implicit/explicit line, the agent-self/character split, the anti-submission loop framed as a security property, the IMMUTABILE-with-DKIM governance model. We publish them as canonical English artifacts in `templates/persona-interview/doctrine-en.md` and invite adversarial review.

The system has been implemented end-to-end on the JuliusCaesar framework. The complete artifact set comprises ~10,000 lines of code, configuration, doctrine, and tests across six implementation phases. One populated instance (Mario Leone, COO of Omnisage LLC) is in operational use as the lead-user reference. Empirical evaluation is in progress.

---

## A. Artifact map

For a researcher reading the codebase, the relevant entry points:

| Concern | Artifact |
|---|---|
| Spec and architecture | `docs/specs/persona-system.md` |
| This research write-up | `docs/research/persona-system.md` (this file) |
| Canonical English doctrine | `templates/persona-interview/doctrine-en.md` |
| Macro vocabulary contract | `lib/persona_macros.py` (`CANONICAL_MACROS`) |
| Question bank | `templates/persona-interview/questions.yaml` |
| Slot id + heading map | `templates/persona-interview/slot-overrides.yaml` |
| Sync (framework template ← reference) | `scripts/sync_persona_template.py` |
| Self-model loop | `lib/self_model/` (8 modules) |
| Frozen-section registry | `lib/self_model/frozen_sections.py` |
| Interview engine | `lib/persona_interview/` (5 modules) |
| Operator CLI | `bin/jc-persona` |
| Tests (105 currently) | `tests/persona/` |
| Lead-user reference instance | `/opt/mario_leone_coo/` (private) |

## B. Notes on this draft

This document is marked *draft* and has been authored alongside the implementation rather than after it. We expect three kinds of revision before submission:

- **Citation completion.** `[CITATION NEEDED]` markers in §2 (Related work) require identifying the specific prior literature in coherent-identity dialogue, AI honesty doctrine, constitutional approaches to alignment, and self-modifying agents.
- **Doctrine review pass.** The four-zone doctrine and §0.2 distinction should be reviewed by an independent AI-safety reader for cases the authors have not anticipated.
- **Empirical results.** §6.1 names the metrics we plan to report; the document should be updated when results are in.

Suggestions to: [CONTACT NEEDED — Filippo or designated correspondence channel].
