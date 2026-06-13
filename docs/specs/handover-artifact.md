# Handover artifact

**Status:** proposed
**Date:** 2026-06-13
**Scope:** specification only; no implementation in this PR
**Touches:** named workers, gateway dispatch + recovery, supervisor, compaction,
transcripts, `jc` CLI, The Company task channel
**Related:** `docs/specs/context-aware-session-lifecycle.md` (checkpoint /
rotation), `docs/specs/conversation-transcripts.md`,
`docs/specs/supervisor-action-background-race.md`
**Origin:** external prior art — handover skill by @vedovelli
(`gist.github.com/vedovelli/6200225227eb0f801517ebd52e825788`), adapted for a
headless multi-host fleet.

## 0. Summary

A **handover** is a forward-looking record of *live execution state*: where a
task is, what was done, what is blocked, and — critically — the exact **first
action** a fresh brain should take to continue. It is a baton, not an archive.

This is distinct from everything JC already persists:

| Store | Holds | Lifetime |
|-------|-------|----------|
| L1/L2 memory | durable facts, identity, learnings | permanent |
| transcripts | what was said, turn by turn | permanent, append-only |
| native session | provider working memory | bounded, rotated |
| **handover** | **in-flight task state + next action** | **ephemeral; expires on resume** |

The gap handover fills, in one example:

```text
Memory:    The Company prod is on 192.168.16.229.   (durable — keep forever)
Handover:  Changed Organization.tsx, not yet deployed. Build passed.
           FIRST ACTION: rsync frontend/dist to /var/www/the-company,
           then verify /organization?company=bnesim.   (baton — discard on resume)
```

Memory must never absorb the second one. It is operational, not durable.

## 1. Incident class

Two failures this artifact addresses, both observed on 2026-06-13:

1. **Worker mid-death black hole.** A worker installing WebDAV for an agent
   hung and was killed before it logged the credential it had just generated.
   The next session had no trail — the password was unrecoverable and had to be
   reset from scratch. Cost: a lost credential and a manual rebuild.

2. **Silent dispatch leaves no trace.** A dispatch ran 6.5 min and returned no
   text; the supervisor progress card was deleted on completion, so the user
   saw nothing and assumed the agent was dead. (Tracked separately in
   `supervisor-action-background-race.md`; handover gives the post-mortem its
   evidence.)

The common root: **when execution is interrupted, intent and progress are not
durably captured anywhere a fresh brain can read them.** The next session
reconstructs state from transcript + git diff + logs — archaeology, every time.

## 2. The dead-process constraint (design-critical)

The naive design — "generate a handover on exit / on interrupt" — **does not
work for the highest-value case**, because a killed or hung process never
reaches its exit hook. The brain is already gone; it cannot write its own
baton.

Therefore handover generation has **two distinct producers**, and the spec
mandates both:

- **A. Incremental self-write (live producer).** The agent rewrites
  `handover.md` at each *phase gate* of a long task — after a step it would not
  want to repeat. The last good checkpoint survives an abrupt kill because it
  was already on disk before death. This is the only mechanism that covers
  mid-death.

- **B. Reconstruction (external producer).** When the gateway/supervisor
  detects a dead child (lease expiry, non-zero exit with no reply, crash), it
  *reconstructs* a best-effort handover from transcript tail + `git
  status`/`git diff --stat` + recent commands + the originating event. Lower
  fidelity than A, but never absent.

A is primary for workers; B is the safety net for everything. An on-clean-exit
write is a nice-to-have, not the design center — do not build only that.

## 3. Non-goals / scope guards

- **Do not duplicate compaction.** "Context getting long" is already handled by
  the session-lifecycle checkpoint/rotation in
  `context-aware-session-lifecycle.md`. Handover is the **format that
  checkpoint emits**, not a second summarizer running beside it. Wire compaction
  to write a handover; do not add a parallel trigger.
- **No clipboard, no desktop notification.** The prior-art skill ends in
  `pbcopy` + macOS `osascript`. Useless on a headless fleet. Drop entirely;
  delivery is file + optional channel attach.
- **Handover is not memory.** It must never be promoted into L1/L2. It expires.
  A resumed session reads it, acts, and the baton is spent.

## 4. Artifact format

Markdown, written to:

```text
state/handovers/<iso-date>-<short-slug>.md      # e.g. 2026-06-13-livia-webdav.md
```

`state/handovers/latest` is a symlink to the most recent file per task scope.

### 4.1 Core sections (all required; "none" is a valid value)

```text
# HANDOVER — <task / title>

WHERE WE ARE
WHAT WAS DONE
CURRENT BLOCKER OR NEXT STEP
TECHNICAL CONTEXT          (exact paths, hashes, errors — no paraphrase)
REPO STATE                 (branch, dirty files, last commit)
VALIDATION RUN             (what was tested, result)
KNOWN DEAD ENDS            (what was tried and failed — saves the next brain the loop)
PLAN FOR NEXT SESSION
FIRST ACTION               (the single command/step to run first)
```

`FIRST ACTION` is mandatory and must be executable, not a description. It is the
whole point: a resumed brain reads handover → runs FIRST ACTION → continues,
without re-deriving state.

### 4.2 JC-specific blocks (include when applicable)

```text
COMPANY TASK STATE
  task id · owner agent · status · accepted_at / finished_at · last comment

GATEWAY / WORKER STATE
  instance · active worker id · originating event id · logs checked · resume risk

DEPLOYMENT STATE
  host · service · last restart · health check
```

`resume risk` flags whether blindly resuming could re-apply a destructive or
already-superseded action (the replay hazard seen recovering Sergio's dev
thread — interdependent edits against a live repo).

## 5. CLI surface

```text
jc handover create [--task <id>] [--slug <name>]
    Collect git branch/status/diff summary, recent commands from the active
    transcript, active task id, changed files, validation/deploy status, and
    an explicit FIRST ACTION. Write state/handovers/<date>-<slug>.md, update
    `latest`. With no FIRST ACTION derivable, prompt the producer to supply one
    (a handover without a first action is rejected).

jc handover latest [--task <id>]
    Print the most recent handover (path + body). Used by resume context.

jc handover attach --task <id>
    Post the latest handover as a comment on the named Company task.

jc handover send-telegram <file> [--chat <id>]
    Deliver a handover to a chat on explicit request only.
```

`create` is the minimum useful version; `latest` is needed for resume;
`attach`/`send-telegram` are opt-in delivery, never automatic.

## 6. Generation triggers

| Trigger | Producer | Mechanism |
|---------|----------|-----------|
| Long worker task, per phase gate | A (self) | worker calls `jc handover create` at each checkpoint |
| Worker interrupted / killed / hung | B (reconstruct) | gateway recovery detects dead child, reconstructs from transcript+diff |
| Session compaction / rotation | A (self) | lifecycle checkpoint emits handover format (no new trigger) |
| Task blocked, awaiting input | A (self) | agent writes before yielding |
| User says "handover" / "continue later" | A (self) | explicit `jc handover create` |
| Before handing work to another agent | A (self) | `jc handover create` then `attach` |

Note: every trigger except the kill case assumes the agent is alive. The kill
case (row 2) is exactly why producer B is mandatory, not optional.

## 7. Resume integration

On resume of a task that has a handover, the runtime injects
`jc handover latest` output into the brain's opening context, framed as *live
state, act on it* — not as durable memory to relitigate. The brain's first move
is FIRST ACTION. On successful continuation the handover is considered spent;
the next checkpoint overwrites it.

## 8. Open questions

1. **Phase-gate granularity.** Who decides what a "phase gate" is — the worker
   prompt, a skill convention, or a runtime heuristic? Leaning: skill/worker
   convention, with a documented "checkpoint before any irreversible or
   credential-producing step" rule (the Livia lesson).
2. **Reconstruction fidelity (producer B).** How much can be recovered from
   transcript + git diff alone when the brain wrote nothing? Needs a spike.
3. **Retention.** When are spent handovers garbage-collected — on next
   checkpoint, on task close, or by an age sweep in `hot_tidy`-style
   maintenance?
4. **Overlap with goal.json.** Workers already carry a `goal.json`. Does
   handover subsume part of it, or sit beside it? Reconcile before implementation.

## 9. Implementation note

Implementation is out of scope for this PR (specs-first). Suggested sequencing
when greenlit: (1) format + `jc handover create`/`latest`, (2) worker
phase-gate self-write, (3) gateway reconstruction on dead-child detection,
(4) wire compaction to emit the format, (5) `attach`/`send-telegram`.
