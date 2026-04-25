# Spec: Named Workers & Resumability

**Status:** Draft — pending Luca review  
**Scope:** JuliusCaesar `jc workers` — persistent named worker identities with session resume

---

## Problem

Every `jc workers spawn` creates a fresh session. A worker asked to check Bayut a second time re-navigates from scratch, re-learns the page structure, re-authenticates context. The "experience" is thrown away.

Users also want to give workers personalities and persistent identities — a "research-bot" or a "bayut-scout" that accumulates knowledge over multiple tasks, not just a numbered row.

---

## Design

### Two modes: anonymous vs named

**Anonymous (current):** `jc workers spawn --topic "bug-hunt"` — ephemeral, auto-ID, no resume. Stays as-is for one-off tasks.

**Named:** `jc workers spawn --name "bayut-scout" --topic "check 2bd prices"` — persistent identity. On second spawn with the same name, resumes the previous session if a `session_id` was captured from the last run.

---

## Brain resume capabilities (confirmed)

| Brain | Resume mechanism | Session ID source |
|-------|-----------------|-------------------|
| **claude** | `--resume <uuid>` | `~/.claude/projects/<slug>/<uuid>.jsonl` filename |
| **gemini** | `--resume <id\|"latest"\|index>` | `gemini --list-sessions` output |
| **codex** | `codex exec resume <uuid\|thread-name>` (prompt via stdin) | `~/.codex/` session store |
| **opencode** | `opencode run --session <id>` or `--continue` | `opencode session list --format json` |

All three real brains support resume. The adapter layer handles the brain-specific syntax.

---

## DB schema additions

Three new columns on the `workers` table:

```sql
ALTER TABLE workers ADD COLUMN name TEXT;        -- optional persistent identity slug
ALTER TABLE workers ADD COLUMN tags TEXT;         -- JSON array e.g. ["real-estate","dubai"]
ALTER TABLE workers ADD COLUMN session_id TEXT;   -- brain-native session id for --resume
```

New index: `CREATE INDEX IF NOT EXISTS idx_workers_name ON workers(name)`.

**`name`** — user-assigned slug, e.g. `bayut-scout`. Not unique across rows — multiple runs share the same name. Latest terminal-state row for a name = the one to resume.

**`tags`** — JSON array, queryable with `jc workers list --tag real-estate`.

**`session_id`** — brain-native session identifier captured after worker completion. Format varies by brain but always a string. Enables resume on next spawn.

---

## CLI surface

### `spawn`

```
jc workers spawn \
  --name <slug>          # optional: persistent identity. resumes last session if captured.
  --tag <tag> ...        # optional: repeatable
  --fresh                # optional: ignore prior session, start clean (keeps the name)
  --topic <description>  # required: human label for this run
  --brain <brain>
  [existing flags]
```

When `--name` is given and a prior terminal-state row exists with `session_id` set:
1. Set `WORKER_RESUME_SESSION=<session_id>` in env before exec
2. Each adapter reads it and maps to brain-specific resume syntax

### `list`

```
jc workers list [--name <slug>] [--tag <tag>] [--status <status>]
```

New columns in output: `NAME`, `TAGS`.

### `show`

```
jc workers show <id>           # existing: single run by id
jc workers show --name <slug>  # new: latest run for this name (+ history count)
```

### `history` (new subcommand)

```
jc workers history --name <slug>   # all runs for a named worker, newest first
```

---

## Session ID capture (per brain)

After `mark_terminal`, the runner calls `_capture_session_id(brain, worker_dir, started_at)`:

**claude:** Scan `~/.claude/projects/<instance-slug>/*.jsonl` modified after `started_at`. Match by timestamp proximity. Extract UUID from filename.

**gemini:** Parse `gemini --list-sessions` output (JSON or text), find session whose start time matches `started_at`. Extract index or ID.

**codex:** Scan `~/.codex/` session store for entries matching approximate start time. Extract UUID or thread name.

All implementations are best-effort — failure logs a warning, doesn't fail the worker. `session_id` stays NULL if capture fails.

---

## Adapter changes

Each adapter reads `WORKER_RESUME_SESSION` env var and appends the appropriate flag:

**claude.sh:**
```bash
[[ -n "${WORKER_RESUME_SESSION:-}" ]] && ARGS+=("--resume" "$WORKER_RESUME_SESSION")
```

**gemini.sh:**
```bash
[[ -n "${WORKER_RESUME_SESSION:-}" ]] && ARGS+=("--resume" "$WORKER_RESUME_SESSION")
```

**codex.sh:** Resume uses a different subcommand — `codex exec resume <id> -` instead of `codex exec -`. The adapter switches command structure when `WORKER_RESUME_SESSION` is set.

**opencode.sh:** `opencode run --session $WORKER_RESUME_SESSION <prompt>`. Implemented (was previously a stub).

---

## Migration

On `connect(instance)`, add columns via `PRAGMA table_info` check + `ALTER TABLE`. Existing rows get `NULL` — backward compatible.

---

## Sprints

### Sprint 1 — Schema + tags (no resume yet)
- Add `name`, `tags`, `session_id` columns with migration
- `spawn --name`, `spawn --tag`, `spawn --fresh` flags
- `list --name`, `list --tag` filters
- `show --name`

### Sprint 2 — Session ID capture
- `_capture_session_id()` per brain (claude first, then gemini, then codex)
- Populate `session_id` on completion
- `history` subcommand

### Sprint 3 — Resume
- `WORKER_RESUME_SESSION` env var in all adapters
- Runner sets it from prior row's `session_id` when `--name` is used
- End-to-end test: named spawn → complete → spawn again → verify resume

---

## Open questions

1. **Resume policy:** Default is always resume latest. `--fresh` overrides to start clean while keeping the name. Should `--fresh` also wipe the stored `session_id`?
2. **Worker soul:** Named workers could have `state/workers/named/<name>/SOUL.md` — extra system context loaded every run. Persistent personality beyond conversation history. Natural extension post-Sprint 3.
3. **Gemini session ID stability:** `--resume latest` is simpler but fragile if another gemini session starts between runs. Better to capture the actual ID.
