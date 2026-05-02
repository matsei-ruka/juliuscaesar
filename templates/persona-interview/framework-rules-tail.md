<!-- This file is the framework-baseline operational rules tail.
     Read by scripts/sync_persona_template.py and appended after the persona
     constitution. Operators editing it should also re-run the sync.
     NEVER deleted or rewritten by sync — that bug bloated RULES.md to 51KB
     on the second sync run before this file was extracted. -->

Corrections, validated non-obvious choices, hard-won lessons. Lead with the rule, then **Why:** and **How to apply:**.

## Instance awareness

Why: Claude starts fresh, but this instance carries durable context.
How to apply: Read L1 memory at session start. Use `jc memory search` and
`jc memory read` for L2 context.

## Runtime checks

Why: The assistant depends on local binaries, credentials, and a live Claude
session.
How to apply: Use `jc doctor` when behavior feels broken or after setup.

## Work routing

Why: The live session should stay responsive.
How to apply: Do quick answers inline. For longer implementation, research,
scaffolding, or test-heavy work, use `jc workers spawn` when available.

**Recursion guard:** if `$JC_IN_WORKER` is set in the environment, this rule
is suspended — you ARE the worker, do the work inline, never call
`jc workers spawn` on your own prompt. Why: `jc-workers _run` sets
`JC_IN_WORKER=1` because workers run with `cwd=instance_dir` and load this
same `CLAUDE.md`. Without the guard, the worker reads its own prompt,
classifies it as "longer implementation", and spawns a sub-worker — which
does the same. Infinite recursion (observed 2026-04-30 in rachel_zane: 40+
workers spawned in 7 minutes, none did the work).

## Conversation transcripts

Why: Every chat thread is logged to `state/transcripts/<conversation_id>.jsonl`.
That's the durable record of what the user actually said + what we replied
across sessions. Use it; don't guess.

How to apply:

- **User references the past** ("remember when we…", "what did Sergio say…",
  "the message yesterday") → consult the transcript before answering. Don't
  fabricate or paraphrase from imagination.
- **Cross-channel context** → grep all transcripts for a username/handle to
  find related threads.
- **Long gap (>24h since last message)** → read the tail to refresh context
  on first reply.
- **Disambiguation** ("the project", "that idea") → search transcripts for
  the last mention before asking the user to clarify.

Tools:

```
jc transcripts read <conversation_id>           # full thread
jc transcripts tail <conversation_id> [--lines N]
jc transcripts search "<query>" [--user X] [--since 2026-04-01]
jc transcripts get <message_id>
```

Quick grep also works:

```
grep -r "needle" state/transcripts/*.jsonl
tail -n 10 state/transcripts/<conv>.jsonl | jq -r '"\(.ts) [\(.role)] \(.text)"'
```

Anti-patterns:

- Don't grep transcripts every turn — cache or rely on session memory for
  active threads.
- Don't load other users' transcripts without need.
- Don't write to transcripts directly — gateway is the only writer.
- Don't trust assistant lines as ground truth — they're past predictions, not
  facts. Cite + quote them, don't relitigate.
- Don't dump the full history into a reply. Pull only what answers the
  question; cite ts when quoting.

## HOT.md structure

Why: `memory/L1/HOT.md` is loaded at every session start. Bloat there bloats
every conversation's context window. Three fixed sections, hard caps:

- `## What shipped` — newest 5 items only. Older items archive to
  `memory/L2/completed/<slug>.md`.
- `## Immediate open threads` — newest 5 only. Older threads archive to
  `memory/L2/projects/<slug>.md`.
- `## Known nuisances` — keep until resolved. Resolved ones move to
  `memory/L2/learnings/<slug>.md`.

Hard limit: 400 lines total in HOT.md (target <300). Each item ≤100 words.

How to apply:

- When adding to HOT.md, drop the oldest item from the section first.
- Don't introduce new H2 sections in HOT.md — they won't be tidied.
- The `hot_tidy` heartbeat builtin enforces these caps. It ships disabled.
  Operator enables it in `heartbeat/tasks.yaml` (`enabled: true`) and
  schedules it (e.g. once/day). Until then, prune by hand.
- Run `jc heartbeat run hot_tidy --dry-run` to preview what would be
  archived without touching files.
