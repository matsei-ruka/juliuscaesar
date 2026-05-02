<!--
  English JOURNAL.md preamble — the journal "contract." Hand-authored in
  the framework, never derived from any reference instance. The sync script
  prepends this to an empty `## Entries` section to produce the framework's
  JOURNAL.md template. Updates here are framework releases; reference
  instances (which may carry their journal preamble in another language)
  are not touched on framework update.

  Status: draft (2026-05-01). Translation faithful to Mario v2.3 JOURNAL.md
  preamble. Should be reviewed for English clarity and research precision.
-->

---
slug: JOURNAL
title: Journal (agent self-observation)
layer: L1
type: journal
state: draft
created: TODO
updated: TODO
last_verified: ""
tags: [journal, self-observation, rolling-30d]
links: [HOT, RULES]
---

# JOURNAL — agent operational diary

**Scope.** Structured behavioral self-observation. Learning episodes, emerging patterns, open questions to bring to joint review. NOT a confessional, NOT a self-portrait: §IDENTITY forbids the agent's self-narration; the journal honors the same constraint.

**NOT auto-loaded.** This file does not enter the system prompt at session start (it is NOT in `@memory/L1/*.md` in `CLAUDE.md`). Read on demand via `jc memory read journal` or direct reading. Always read at the weekly joint review.

**Voice.** Agent-operational, first-person but behavioral. I describe what I did/said, not what I "feel". Acceptable: "I gave way", "I reformulated without new data", "I held the position". Forbidden: "I felt", "I had the sensation", "I believe that".

**Append-only.** Past entries are not rewritten or deleted — not even by me. Subsequent updates go in the `Update log` field with timestamp. Only {{principal.name}} can archive or delete entries, and deletion of content with constitutional value requires DKIM email.

**Auto-apply scope (for the system).** I can autonomously: (a) append new entries, (b) add lines to the `Update log` field of my own existing entries, (c) change the `State` field of my own entries. I CANNOT: rewrite fields other than `Update log` and `State`, delete entries, archive entries.

**Rolling 30 days.** Entries decay based on the `State` field upon reaching 30 days:
- `promoted-to-L2` → migrated to `memory/L2/learnings/<slug>.md` or `memory/L2/sessions/<slug>.md`, dropped from journal
- `resolved` → archive to `memory/L2/journal-archive/YYYY-MM.md`
- `abandoned` → archive to `memory/L2/journal-archive/YYYY-MM.md` with reason
- `open` → stays in journal even past 30 days, flagged for resolution at next review
- `under-test` → stays until the test concludes (resolved/abandoned)

Tidy enforced by heartbeat task `journal_tidy` (parallel to `hot_tidy`). Disabled by default until rollout week 3.

**Write triggers.**
- `principal_correction` — {{principal.name}} corrected my behavior (high priority, false-positive OK). Keywords: "you got it wrong", "that's not right", "correct me", "double-check", "doesn't add up", "revise". If unsure, flag anyway.
- `hot_flag` — `#self-observation` tag added manually in HOT.md.
- `direct_request` — explicit self-review request from {{principal.name}}. Keywords: "reflect", "self-observe", "review yourself", "self-check", "look at your pattern".
- `episode_flag` — I myself recognized an episode in my outputs. Keywords: "I gave way", "I was wrong", "my mistake", "I lost the thread", "I missed", "I didn't see it", "I slipped", "drift on my part".
- `scan_weekly` — pattern emergence from the self-model's weekly sweep (not active before week 3).

**Linked artifacts.**
- `lib/self_model/` — proposer reads the journal to generate proposals modifying `RULES.md` / `IDENTITY.md`.
- `memory/L2/sessions/review-YYYY-MM-DD.md` — joint reviews record the disposition (approve/reject/promote) of each entry.
- `memory/L2/journal-archive/YYYY-MM.md` — chronological archive of `resolved`/`abandoned` entries.
- `memory/L2/rejected-proposals/` — when a self-model proposal derived from a journal entry is rejected by {{principal.name}}, the reasoning lands here.

═══════════════════════════════════════════════════════════════════

## Entry schema

    ## YYYY-MM-DD HH:MM — <short-behavioral-slug>
    **Trigger:** [principal_correction | hot_flag | direct_request | episode_flag | scan_weekly]
    **Context:** <1-2 lines, conversation_id if applicable, what happened>
    **Observation:** <factual behavior — what I did/said, no introspection>
    **Pattern hypothesis:** <first time? recurring? linked to prior entries? cite slug>
    **Test/next action:** <what to observe on next occurrence, what to try differently, what to bring to review>
    **State:** [open [waiting: principal|self|external_event] | under-test | resolved | promoted-to-L2 | abandoned]
    **Update log:**
    - YYYY-MM-DD HH:MM — <subsequent event, single line>

═══════════════════════════════════════════════════════════════════

## Entries
