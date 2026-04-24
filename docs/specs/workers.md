# Spec: `jc workers` — on-demand background agents

**Status:** draft
**Author:** Rachel (under Luca's direction)
**Created:** 2026-04-24
**Branch:** `workers`
**Tracking PR:** _not yet opened_

---

## 1. Problem

Today, JC instances run a single interactive Claude Code session that serves Telegram. When the user asks for substantive development work, that same session executes it — which has three downsides:

1. **Blocking**: the session is unresponsive during dev work. The user can't chat, can't ask about other topics, can't get fast answers to quick questions.
2. **Context contamination**: unrelated topics share one transcript. A refactor PR and a real-estate question end up interleaved, growing context and degrading focus.
3. **Model/cost tradeoff**: for chat we want fast + cheap (Sonnet). For heavy reasoning we want Opus. With one session, the user must pick — and pays for Opus tokens even on trivial exchanges.

## 2. Goal

A background agent pool that decouples *chat* from *work*:

- The main session stays small, fast, always-on — ideally Sonnet for snappy responses.
- Dev tasks spawn as **workers**: background processes running on a chosen brain (Claude Code, Codex, Gemini, OpenCode) and model, with their own context and lifecycle.
- State is tracked in SQLite so the main session (and the user) can query "what's running, what finished, what broke" at any time.
- Completion is pushed to Telegram so the user gets notified without polling.

## 3. Non-goals

- **Not a replacement for the heartbeat system.** Heartbeat is cron-driven scheduled tasks; workers are user-triggered on-demand tasks. They share the adapter layer but diverge above it.
- **Not a gateway replacement for inbound Telegram.** Main session still handles inbound. Workers are outbound-only.
- **No interactive worker-user bidirectional clarification (MVP).** If a worker needs info mid-task, it fails with a `need_input` status; user refines the prompt and respawns. (Future: inbound routing per-worker.)
- **Not multi-user.** Worker ownership isn't tracked per user for now; that's an orthogonal axis.

## 4. Architecture

```
  Main session (Claude Code, Sonnet, always-on)
      │
      │  spawn(topic, brain, model, prompt)
      ▼
  jc-workers CLI ──────► state/workers.db (SQLite)
      │                      ▲
      │ setsid + nohup        │ status updates
      ▼                      │
  Worker process ───────────┘
  (claude -p | codex exec | gemini -p | opencode ...)
      │
      │ on completion
      ▼
  send_telegram.sh  ──►  user's Telegram DM
```

**Key properties:**

- Workers are **non-interactive**. They use existing adapters (`lib/heartbeat/adapters/*.sh`) that read stdin and write stdout. No PTY juggling.
- Workers are **detached**. `setsid nohup` so they survive the shell that spawned them. If the main Claude Code process dies (auto-update, crash), in-flight workers keep going.
- Workers are **scoped to their instance**. The worker DB, logs, and results all live under `<instance>/state/workers/`. Multi-instance hosts don't cross-contaminate.
- **Main session is the spawner, not the worker.** The main Claude Code reads the DB to report status; it never blocks on worker completion.

## 5. Data model

### `state/workers.db`

```sql
CREATE TABLE workers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic           TEXT NOT NULL,                 -- short human label ("refactor auth")
    brain           TEXT NOT NULL,                 -- claude | codex | gemini | opencode
    model           TEXT,                          -- optional model override
    prompt_path     TEXT NOT NULL,                 -- path to prompt file (so we can resend on retry)
    status          TEXT NOT NULL,                 -- queued | running | done | failed | cancelled | need_input
    pid             INTEGER,                       -- OS pid while running (NULL otherwise)
    exit_code       INTEGER,                       -- set on terminal states
    log_path        TEXT NOT NULL,                 -- state/workers/<id>/log
    result_path     TEXT,                          -- state/workers/<id>/result (final stdout)
    spawned_by      TEXT,                          -- 'main' | 'cron' | 'user' (free-form)
    telegram_msg_id TEXT,                          -- the inbound user msg that triggered the spawn
    notify_chat_id  TEXT,                          -- which chat to notify on completion
    started_at      TEXT NOT NULL,                 -- ISO 8601 UTC
    finished_at     TEXT,                          -- ISO 8601 UTC, NULL while running
    error           TEXT                           -- last stderr line if failed
);

CREATE INDEX idx_workers_status ON workers(status);
CREATE INDEX idx_workers_started ON workers(started_at DESC);
```

### Filesystem layout

```
<instance>/
  state/
    workers.db
    workers/
      1/
        prompt           # the input prompt (plaintext)
        log              # full stdout+stderr merged, unbounded
        result           # final stdout only (may equal log)
        meta.json        # same row as DB, for debugging
      2/
        ...
```

Prompt stored as a file (not in DB) to keep DB small and allow easy re-run via `cat prompt | adapter`.

## 6. CLI surface

### `jc workers spawn`

```
jc workers spawn --topic <label> --brain <name> [--model <id>] [--notify <chat_id>] [--prompt <file> | -]
```

- `--topic`: short label for display (required)
- `--brain`: adapter name (required)
- `--model`: optional model override
- `--notify`: optional Telegram chat_id override (default: `$TELEGRAM_CHAT_ID`)
- `--prompt`: path to prompt file, or `-` to read from stdin

**Behavior:**
1. Insert row with `status=queued`.
2. Copy prompt to `state/workers/<id>/prompt`.
3. Fork a detached process: `cat prompt | adapter <model> > result 2>> log; <update DB>; <send telegram>`.
4. Return immediately with: `spawned worker #<id> (pid <pid>)`.

### `jc workers list`

```
jc workers list [--status running|done|failed|cancelled|all] [--limit N]
```

Tabular output:
```
ID   STATUS    BRAIN    MODEL           STARTED              TOPIC
42   running   claude   opus-4-7        2026-04-24 12:30Z    refactor auth middleware
41   done      codex    gpt-5.4         2026-04-24 11:15Z    script to dedupe CSV
40   failed    claude   sonnet-4-6      2026-04-24 10:02Z    LinkedIn scraper
```

### `jc workers show <id>`

Full details for one worker (row + result preview).

### `jc workers tail <id>`

Streams the worker's log live (`tail -f`). Useful for watching a running task.

### `jc workers cancel <id>`

`kill -TERM <pid>` then after grace `kill -9`. Mark `status=cancelled`.

### `jc workers gc`

Remove completed/cancelled workers older than N days. Safety: keeps `log` and `result` unless `--prune-files` passed.

## 7. Worker lifecycle

```
queued  ──spawn──►  running  ──exit 0──►  done          (notify: summary + result preview)
                      │
                      ├──exit != 0──►  failed           (notify: last stderr line)
                      │
                      ├──cancel────►  cancelled         (notify: cancelled by user)
                      │
                      └──"NEED_INPUT" token in output──►  need_input  (notify: prompt excerpt)
```

### Completion notification format

When a worker reaches a terminal state, it calls `send_telegram.sh` with:

```
worker #42 done (14 min, 2.3k tokens)
topic: refactor auth middleware
brain: claude / opus-4-7

<first 500 chars of result>

full: /abs/path/to/result
```

### NEED_INPUT sentinel

If a worker's final stdout starts with `NEED_INPUT:`, the spawner marks status as `need_input` and the notification surfaces the question. User's next chat with main can reference the worker id; main reads the prompt + question, composes a follow-up prompt, respawns.

(MVP: treated identically to `failed` for now. Sentinel handling is a Sprint 2+ feature.)

## 8. Main-thread integration

The main Claude Code session gains a lightweight helper:

- **When to spawn**: if the user asks for work that clearly exceeds "quick answer" scope (implementation, refactor, research report, multi-file edits), spawn a worker instead of doing it inline.
- **How to spawn**: via a single bash command (no multi-step interaction): `jc workers spawn --topic "..." --brain claude --model opus-4-7 --prompt - <<EOF ... EOF`
- **Reporting back**: main's reply quotes the worker id and says "I'll ping when it's done."
- **Status queries**: if the user asks "what are you working on", main runs `jc workers list --status running` and pastes the table.

**Heuristic for spawn vs inline** (guideline, not strict):
- Single edit, one file, ≤ 50 lines → inline
- Multi-file refactor, scaffolding, research → spawn
- Anything with tests to run or that needs iteration → spawn
- Chat, questions, explanations → always inline

## 9. Security

Workers run with the same permissions as the main session (currently `--dangerously-skip-permissions` for Claude Code). The host machine is already trusting them; workers don't escalate.

**Guard:** `jc workers spawn` validates that the prompt file is within the instance directory tree. Prevents accidental exfiltration via `--prompt /etc/passwd`.

## 10. Failure modes

- **Adapter not installed** → fail fast at spawn time, status=failed, error="codex CLI not installed"
- **Brain hangs indefinitely** → `--timeout <seconds>` flag (default: 3600). After timeout, SIGTERM → SIGKILL, status=failed, error="timeout after 3600s"
- **Disk full during log write** → log write fails silently; DB row still updates on exit
- **Parent JC process dies while worker runs** → worker keeps going (detached); on next main-session startup, a `jc workers reconcile` step checks pid liveness and marks orphans
- **DB corruption** → `state/workers.db` is derivable from filesystem (each worker has `meta.json`); a `jc workers rebuild` rebuilds the DB from the filesystem

## 11. Sprints

### Sprint 1 — Schema + spawn (2-3h)
- `lib/workers/db.py` — schema, row CRUD
- `bin/jc-workers` router + `spawn` subcommand
- Spawns via existing adapters, writes row, forks detached process, updates on exit
- No notifications yet; test via `jc workers list`

### Sprint 2 — Queries + notifications (2-3h)
- `jc workers list / show / tail`
- Completion hook: `send_telegram.sh` invoked on terminal states
- First end-to-end test: spawn, get notification when done

### Sprint 3 — Lifecycle (2-3h)
- `jc workers cancel`
- `--timeout` flag
- `jc workers gc` + `--prune-files`
- `jc workers reconcile` for orphan detection on startup

### Sprint 4 — Main-thread integration (1-2h)
- Document spawn heuristic in `docs/ARCHITECTURE.md`
- Add L1 memory rule to Rachel (and `jc init` template) teaching main session when to spawn
- Add `need_input` sentinel parsing (promote from MVP=failed to proper status)

### Sprint 5 — Polish + docs (1-2h)
- `jc doctor` checks: DB schema version, state dir writable, no orphan pids
- `QUICKSTART.md` section on workers
- `ROADMAP.md` updated

Total: ~10-13h. Call it 2-3 focused days.

## 12. Testing plan

- **Unit**: `lib/workers/db.py` round-trip CRUD + edge cases (duplicate ids, nullable columns, JSON serialization)
- **Integration**: shell-script test that spawns a trivial worker (`echo hello | claude -p`), waits for completion, verifies DB + telegram notification
- **Manual**: spawn 3 parallel workers on different brains, confirm none block each other, all complete, all notify
- **Chaos**: kill the main JC process mid-worker, restart, verify worker completes and is visible in list

## 13. Open questions

1. **Result size limits.** If a worker emits 10MB of output, do we paste the first 500 chars to Telegram and store the rest, or paginate? Recommend: truncate notification, full result in `result` file path.
2. **Concurrency limits.** Should there be a max parallel workers cap? Machine has finite CPU/memory. Recommend: soft cap of 5, configurable via `ops/workers.conf`.
3. **Cross-instance locking.** Multi-instance hosts: does each instance have independent worker DBs? (Yes, per PR #7 scoping.)
4. **Prompt templating.** Workers often need access to memory. Should we auto-prepend L1 like heartbeat does? Recommend: yes, with `--no-context` opt-out.

## 14. Future extensions (out of scope for MVP)

- **Worker dependencies**: worker A's output feeds worker B.
- **Scheduled workers**: a cron-triggered one-shot task (bridge to heartbeat).
- **Bidirectional chat with a running worker**: proper PTY so user can clarify mid-task.
- **Cost tracking**: token usage summed in DB, exposed via `jc workers cost`.
- **Multi-user ownership**: `owner_user_id` column; `jc workers list --mine`.
