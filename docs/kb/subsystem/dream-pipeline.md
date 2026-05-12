---
title: Dream pipeline
section: subsystem
status: active
code_anchors:
  - path: lib/dream/runner.py
    symbol: "def run_dream"
  - path: lib/dream/reflect.py
    symbol: "def reflect("
  - path: lib/dream/consolidate.py
    symbol: "def consolidate("
  - path: lib/dream/codify.py
    symbol: "def codify("
  - path: lib/dream/apply.py
    symbol: "def apply_artifacts("
  - path: lib/dream/risk.py
    symbol: "def classify("
  - path: bin/jc-dream
    symbol: "jc-dream"
last_verified: 2026-05-12
verified_by: Matsei Ruka
related:
  - subsystem/commitments-and-reengagement.md
  - subsystem/memory-system.md
  - subsystem/heartbeat-runner.md
---

## Summary

Dream is the offline reflection and self-improvement cycle. `jc dream tick`
collects recent transcripts, memory state, sent heartbeat records, and closed
commitments; consolidates signals and memory hygiene findings; codifies those
findings into proposed artifacts; applies low/medium-risk artifacts; stages
sensitive artifacts; and writes an audit report under `state/dreams/`.

The implementation intentionally wraps existing self-model detectors instead of
renaming or replacing `lib/self_model/`. In v1, the codify phase is deterministic
and auditable: it emits playbooks, learnings, backlink stubs, and verification
commitments from local signals. It does not require a nightly LLM call to be
useful.

## Phases

1. `reflect`: compute the dream window, transcript deltas, memory frontmatter
   hash, sent heartbeat deltas, and closed commitments.
2. `consolidate`: run self-model signal detectors and memory hygiene checks
   for duplicates, broken wikilinks, and stale live-world entries.
3. `codify`: emit proposed artifacts through dedicated emitters.
4. `apply`: auto-apply LOW/MEDIUM artifacts, stage SENSITIVE artifacts, reject
   frozen targets, and retain rollback metadata for auto-applied artifacts.
5. `report`: write markdown audit trail with findings and artifact statuses.

## CLI surface

`jc dream` supports:

- `tick`
- `dry-run`
- `run --since <iso> --until <iso>`
- `list`
- `show <dream-id>`
- `pending`
- `approve <diff-id>`
- `reject <diff-id>`

The heartbeat builtin is `dream_tick` and ships disabled in instance templates.

## Artifacts

- `memory/L2/playbooks/*.md`
- `memory/L2/learnings/*.md`
- `memory/L2/stubs/*.md`
- `state/commitments/*.yaml`
- `state/dreams/<UTC>.md`
- staged sensitive diffs under `state/dreams/pending/`
- rollback retainers under `state/dreams/retained/`

## Risk rules

- LOW: reversible hygiene such as backlink stubs. Auto-applied.
- MEDIUM: new L2 content or commitments. Auto-applied with retained rollback.
- SENSITIVE: L1 files and identity/rules-style surfaces. Staged only.
- IMMUTABILE or `<!-- FROZEN -->` targets are rejected before staging.

## Invariants

- Dream never mutates transcripts.
- Dream report files are append-only audit artifacts.
- `jc dream reject <diff-id>` can reject staged diffs or roll back retained
  auto-applied artifacts.
- `lib/self_model/` remains the signal/proposal engine contract; dream is the
  orchestrator and artifact writer around it.

## Open questions / known stale

- 2026-05-12: V1 codify is deterministic. A managed or local LLM codify backend
  can be added later behind the same `Reflection -> ProposedArtifact` boundary.
