# Migration: transcripts + HOT.md doctrine for existing instances

This release ships two new memory features:

1. **Per-conversation transcripts** — gateway logs every inbound user message
   and outbound assistant response to `state/transcripts/<conversation_id>.jsonl`.
2. **HOT.md size management** — `hot_tidy` heartbeat builtin enforces
   per-section caps and archives overflow to `memory/L2/`.

Both ship transparently for new instances scaffolded from
`templates/init-instance/`. Existing instances need a one-shot manual merge
of doctrine into their `memory/L1/RULES.md`.

## What's automatic on upgrade

After re-installing the framework (`./install.sh`) on an existing instance:

- `state/transcripts/` is created on first message and gitignored already
  (it lives under `state/`, which the default `.gitignore` excludes).
- The gateway hooks for inbound + outbound logging activate at startup. No
  config required.
- `jc transcripts` CLI is on `$PATH`.
- `hot_tidy` is **not** auto-enabled. It only runs when the operator adds a
  task entry — see step 2 below.

## What you need to do manually

### 1. Merge doctrine into `memory/L1/RULES.md`

Open your existing `memory/L1/RULES.md` and append these two sections (or
copy them from `templates/init-instance/memory/L1/RULES.md`):

````markdown
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
  facts.
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
````

### 2. (Optional) Enable `hot_tidy` in `heartbeat/tasks.yaml`

Add this task block to your instance's `heartbeat/tasks.yaml` under
`tasks:` if you want automatic archival:

```yaml
  hot_tidy:
    builtin: hot_tidy
    enabled: false   # set true to commit changes; false runs as dry-run
```

Then preview what it would do:

```bash
jc heartbeat run hot_tidy --dry-run
```

When you're happy with the plan, flip `enabled: true` and either run it
manually or schedule it via cron alongside your other heartbeat tasks.
Suggested cadence: once a day.

### 3. Align your existing `HOT.md`

If your `memory/L1/HOT.md` doesn't yet match the canonical structure
(`## What shipped`, `## Immediate open threads`, `## Known nuisances`), do
a one-shot reformat first. `hot_tidy` only operates on those three section
names — anything else is left alone. The parser also accepts:

- `## What shipped today` (legacy)
- `## Open threads`
- `## Immediate open threads`
- `## Known nuisances`
- `## Known nuisances (documented)`

It does **not** touch sections it doesn't recognize.

## Rollback

- **Transcripts**: delete `state/transcripts/`. The gateway will skip writes
  if the dir can't be created (best-effort) and recreate it on next message.
  Removing the doctrine block from `RULES.md` stops the agent from consulting
  transcripts at runtime.
- **`hot_tidy`**: remove the task entry from `heartbeat/tasks.yaml`. No
  state to clean up — the L2 archive entries it creates are normal memory
  files and can be deleted, edited, or kept.

## Verification

```bash
# Transcript pipeline
jc transcripts list                                # should print conversations
echo '{"text":"hi"}' | jc gateway enqueue ...      # send a test message
jc transcripts tail <conversation_id>              # confirm the line appears

# hot_tidy preview
jc heartbeat run hot_tidy --dry-run                # prints summary JSON
```
