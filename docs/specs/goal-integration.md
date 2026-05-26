# Goal Integration — Cache-Driven Task Anchor

Status: Design draft (rev 3 — finalized: native-channel delivery, two-class injection), spec-only PR
Date: 2026-05-26
Author: Noah
Repo: `juliuscaesar`
Related:
- `docs/specs/company-inbox-channel.md` (PR #64 — companion #1 of the trilogy, **merged**)
- `the-company` v2.2 task graph (`docs/specs/the-company-v2.2-task-graph.md` in matsei-ruka/the-company)

---

## 0. What this spec is, and is not

This spec defines a **brain-agnostic task anchor**: when the agent is
executing a task, every brain dispatch on that task's conversation is
prefixed with a stable goal block so the model does not drift or need
the task re-explained each turn.

It is **cache-driven prompt injection**, not a native-slash-command
integration. Rev 1 of this spec assumed `claude-code`/`codex` expose a
persistent `/goal` slash command reachable through our adapters, and
that a per-slot `Brain` object holds the goal between turns. **Both are
false in this codebase** (see §0.1). Rev 2 is rebuilt on what actually
exists: a goal cache file, read by `Brain.prompt_for_event` on every
dispatch.

### 0.1 Architecture constraints this spec must respect

These are verified against the current tree — the design follows from
them, it does not wish them away:

1. **Brains are constructed fresh per dispatch.**
   `lib/gateway/brains/dispatch.py:invoke_brain` does
   `instance = cls(instance_dir, override=…)` then `instance.invoke(...)`
   for *every* event. There is no persistent per-slot `Brain` object, so
   instance state (`self._pending_slash`, etc.) cannot survive to the
   next turn. Any goal that "persists across turns" must live **outside**
   the brain instance — on disk.

2. **No `/goal` slash command, but a real native system-steering channel
   exists.** `claude.sh` runs `claude -p` headless; a `/goal …` line would
   be literal prompt text, not an executed slash command. But the harnesses
   *do* expose a native system-steering input, and the framework **already
   uses it**: the "gateway output contract" is injected per-harness via
   `--append-system-prompt` (claude, pi), the API `instructions`/`system`
   slot (codex_api, gemini, openrouter), or — where the CLI has no such flag
   — a block prepended to the prompt body (codex-exec, opencode, aider). The
   goal rides those same channels (§4). This is the mechanism, not a slash
   command and not a user-turn hack.

3. **Adapters receive dynamic values via env vars, not forwarded args.**
   `claude.sh` consumes only `$1` (model) and builds its own arg list — it
   does not forward arbitrary extra args. The established seam for feeding an
   adapter a per-dispatch value is an environment variable: `base.invoke`
   does `env = os.environ.copy(); … env.update(self.extra_env())` and the
   adapter reads it (e.g. `JC_RESUME_SESSION`). So the goal reaches a
   system-prompt-class adapter as an env var (e.g. `JC_GOAL`) that the
   adapter turns into `--append-system-prompt "$JC_GOAL"` — exactly how
   resume id and worker mode are already plumbed.

4. **The durable key is `conversation_id`, not a slot number.** Slot ids
   are assigned per-dispatch by the affinity classifier; they are not
   stable identity. PR #64 sets `conversation_id = "task-root:<root_id>"`
   on every injected task event. That string is the natural, stable key
   for goal state.

5. **There is no `company.task_closed` producer today.** The merged #64
   channel emits only `company.task_assigned`, polls `status=pending,
   accepted`, and does not track terminal flips. The clear-goal trigger
   this spec needs does not yet exist (see §5.3 — the gating dependency).

## 1. Motivation

A task anchor keeps the model on the objective across the multiple
dispatches a multi-step task takes, instead of re-explaining "you are
working on task X" in every prompt (token cost + drift). With the v2.2
task graph live, the mapping is:

> the agent's current goal for a conversation = the task that conversation is executing

Failure mode of record without this: an agent that takes 6 dispatches to
finish a task re-frames it 6 times, and as the conversation grows the
model drifts toward the general chat topic instead of the task.

## 2. Scope

### 2.1 In scope

- A new module `lib/gateway/goal_cache.py`: `set`, `clear`, `get`,
  `all_goals`, backed by `<instance>/state/gateway/goal.json`, keyed by
  `conversation_id`. Single-writer, atomic, multi-reader safe (§3).
- A `format_goal(meta) -> str` helper (in `goal_cache.py` or a small
  util) that builds the goal text from a task-assigned event's `meta`.
  Default `"<title>\n\n<description>"` capped at 500 chars.
- Goal delivery via each task-brain's **native system-steering channel**
  (§4), keyed by `event.conversation_id`, in **two policies** by brain
  class:
  - **System-prompt class** (`claude`, `pi`): `JC_GOAL` env →
    `--append-system-prompt`; re-applied every call (ephemeral, never
    enters the transcript).
  - **Body class** (`codex`-exec, `opencode`, `aider`): goal block
    prepended to the prompt body, **first turn of a session only**
    (`resume_session is None`) so it does not pile up in the resumed
    transcript.
  Stateless brains (`openrouter`, `gemini`, `codex_api`) are not used as
  task executors, so they need no goal handling (§2.2).
- Dispatcher hooks in `lib/gateway/runtime.py`:
  - on a `company.task_assigned` event entering dispatch →
    `goal_cache.set(conversation_id, task_id, format_goal(meta))`.
  - on a `company.task_closed` event → `goal_cache.clear(conversation_id,
    task_id)`.
- Restart safety: the cache is a file, so a gateway restart rehydrates
  goals with no brain-session dependency (no re-issue call needed —
  the next dispatch simply reads the cache).

### 2.2 Out of scope (deferred)

- **Native slash-command integration.** Not viable through the current
  headless adapters (§0.1.2). The native *system-prompt* channel
  (`--append-system-prompt`) is what we use instead; if a CLI later ships a
  true persistent in-session anchor verified under headless `--resume`,
  optimizing that brain is a follow-up.
- **Goal support for stateless brains** (`openrouter`, `gemini`,
  `codex_api`). They are deliberately not used as the task-executing brain
  (`openrouter` is the narrow unsafe-fallback; `codex_api`/`gemini` serve
  triage/chat), and a task anchor only matters for the brain that runs the
  multi-turn task. So no goal handling is added for them — not a gap, a
  scoping decision. If one is ever promoted to task executor, it gets the
  body-class or system-message treatment then.
- **Multi-goal hierarchies** — a parent task with active children on
  separate goals. One goal per conversation; a child spawned onto its own
  `task-root` conversation gets its own goal independently.
- **Goal expiry / time-decay** — the goal stays until `task_closed`
  (or the age-fallback of §5.3) clears it.
- **Custom goal templating** — operators wanting bespoke goal text
  override `format_goal` (it is a plain function/util, not buried in a
  brain subclass — §6).
- **Persona surfacing** — how the persona prompt references the goal is
  PR #3. This spec only guarantees the goal is in the prompt and in the
  event `meta` (§5.4 / Q3).

## 3. Goal cache

Path: `<instance>/state/gateway/goal.json`. Keyed by `conversation_id`:

```json
{
  "task-root:64244b95-…": {
    "task_id": "64244b95-…",
    "text": "Onboard Francesco Datini\n\nRun the prepared script …",
    "set_at": "2026-05-26T07:00:00Z"
  }
}
```

Note the key is `conversation_id` (`task-root:<root_id>`), not a slot id.
Chat conversations (`telegram:…`) never appear here, so `get` returns
`None` and no goal block is rendered for them.

**Concurrency — single-writer, atomic, multi-reader.** All `set`/`clear`
calls happen on the **dispatch-loop thread** (the dispatcher fires them
before handing an event to a slot worker — §5.1), so there is exactly one
writer; no lock or read-modify-write race. Writes use tempfile +
`os.replace` (whole-file), which is atomic. Brain reads (`get`, in
`prompt_for_event`) happen on worker threads but only read, and always
see a coherent old-or-new file — never a torn middle. This is the same
single-writer/atomic-replace discipline the rest of `state/gateway/` uses;
the rev-1 "per-slot POSIX advisory locks" are unnecessary and were
incompatible with `os.replace` (the lock rides the old inode).

`get` tolerates a missing/corrupt file by returning `None` (a goal is an
optimization, never required for correctness).

## 4. Goal delivery (per brain class)

The goal text is brain-agnostic (the cache); **delivery uses each
task-brain's native system-steering channel** — the same channels the
gateway output contract already rides. One cache, one source of truth, but
**two injection policies** because the task brains fall into two classes
with different transcript behavior. Stateless brains are out of scope
(§2.2), so only the resume-capable task brains are covered here.

Shared helper:

```python
# lib/gateway/goal_cache.py
def goal_text(instance_dir, conversation_id) -> str:
    g = get(instance_dir, conversation_id)
    return g["text"] if g else ""
```

### 4.1 System-prompt class — `claude`, `pi`

These adapters already inject directives via `--append-system-prompt`
(worker mode, output contract — see `claude.sh`). The goal is a third such
append:

- `base.invoke` sets `env["JC_GOAL"] = goal_text(instance_dir,
  event.conversation_id)` when non-empty (env is the seam — §0.1.3).
- `claude.sh` / `pi.sh` add `--append-system-prompt "$JC_GOAL"` when
  `JC_GOAL` is set (a ~3-line adapter edit, mirroring the `JC_RESUME_SESSION`
  block).

Re-applied **every** call. The system prompt is ephemeral per invocation —
it never enters the resumed transcript, so there is no accumulation. This
is the clean class. Compose, don't clobber: the goal append is additive
alongside the existing output-contract append; order is goal-last so it is
the most recent steering the model sees.

### 4.2 Body class — `codex`-exec, `opencode`, `aider`

These CLIs expose no system-prompt flag in headless mode (`codex.sh` says
so explicitly; `opencode` takes a positional arg; `aider` takes
`--message-file`), so the goal rides the **prompt body** — exactly where
those adapters already put the output contract.

- Rendered as a `<goal>…</goal>` block prepended in the prompt path
  (`base.Brain.prompt_for_event`, which these brains use unchanged).
- **First turn of a session only** — render the block when
  `resume_session is None`; skip it when resuming. These brains persist the
  body into their resumed session/history (`codex exec resume`, aider
  history files, opencode `--session`), so re-prepending every turn would
  pile the goal up in the transcript (token bloat + the model seeing it
  re-instructed). First-turn injection lands the goal in the transcript
  once; resume carries it thereafter.

### 4.3 Summary

| Brain | Channel | Re-inject policy |
|---|---|---|
| claude, pi | `JC_GOAL` env → `--append-system-prompt` | every call (ephemeral) |
| codex-exec, opencode, aider | prompt-body `<goal>` block | first turn only (`resume_session is None`) |
| openrouter, gemini, codex_api | — (not task executors, §2.2) | — |

There is no `supports_native_goal` flag, no `set_goal`/`clear_goal` on
`Brain`, no `_pending_slash`. The brain reads the cache (directly, or via
the `JC_GOAL` env the runtime sets); lifecycle writes live in the runtime
(§5). This respects §0.1.1: state the brain can't hold does not live on the
brain.

## 5. Task lifecycle integration

The runtime, not the brain, owns set/clear:

```
event company.task_assigned arrives (via company-inbox, PR #64)
   conversation_id = task-root:<root_id>, meta.kind = task_assigned
  → dispatch loop, before handing to a slot worker:
       goal_cache.set(conversation_id, meta.task_id, format_goal(meta))
  → brain applies the goal via its native channel (§4)

subsequent dispatch on the same conversation (multi-turn task)
  → no set call needed; the cache still holds the goal
  → system-prompt class re-applies it; body class relies on resume (§4.2)

task reaches terminal (done | failed | rejected | cancelled | expired)
  → SEE §5.3 — there is no producer of this signal yet
  → when available: company.task_closed event
  → dispatch loop: goal_cache.clear(conversation_id, task_id)
```

### 5.1 Where the hooks live

- `lib/gateway/runtime.py` — in the dispatch path, before a
  `company.task_assigned` event is handed to a worker, call
  `goal_cache.set(...)`. Doing it on the dispatch-loop thread is what
  makes the cache single-writer (§3).
- `goal_cache.clear(...)` fires from the same dispatch path when a
  `company.task_closed` event is processed.

No brain-method call, no per-slot brain lookup (there is no persistent
per-slot brain — §0.1.1).

### 5.2 Conversation semantics

Goal state is per `conversation_id`. A `telegram:*` chat conversation has
no goal; a `task-root:<root_id>` conversation has the task's goal. If the
affinity classifier routes a *chat* event onto a slot currently running a
task, that chat event carries a different `conversation_id` and so reads a
different (likely empty) goal entry — there is no cross-talk and nothing to
"swap." Rev 1's "slot 0 chat vs slot 1 task" framing assumed a fixed slot
layout that does not exist; keying on `conversation_id` removes the
problem entirely.

### 5.3 Gating dependency: the clear trigger does not exist yet

`clear_goal` needs to know a task went terminal. **No such signal exists
today** (§0.1.5): the #64 channel emits only `company.task_assigned`,
polls `pending,accepted`, and a task going terminal simply *disappears*
from that filter — the channel never notices. So this spec **cannot ship
its clear path** until one of these lands (pick one, in review):

- **(A) Extend the company-inbox channel** to detect a previously-seen
  task that has left the `pending,accepted` set (or to additionally poll
  terminal statuses) and emit a synthetic `company.task_closed` event.
  This is a re-scope of #64's implementation — call it out as its own
  small PR.
- **(B) Age-fallback clear** in `goal_cache`: a goal older than a
  configured TTL (default e.g. 1 h) is dropped on the next `get`. Coarse,
  but bounds the leak with zero backend dependency. Can ship alongside (A)
  as a backstop.

Until (A) exists, a set goal would otherwise leak forever. The
implementation PR must land (A) or (B) **before** the set path, or ship
with (B) as the floor.

### 5.4 Belt + braces

The goal is also already in the event `meta` (`kind=task_assigned`, with
`task_id`/`title`/`payload` — emitted by #64). So even on a turn where
the cache read returns empty (corruption, race on first set), the model
still sees the task in the routing metadata. The `<goal>` block is the
persistent anchor; `meta` is the per-event fallback.

## 6. Goal text format

`format_goal(meta) -> str` — a plain helper over a task-assigned event's
`meta`:

```
<title>

<description>
```

Capped at 500 chars (truncate `description`; full body remains in
`meta.payload`). It is a function, not a `Brain` method — the task fields
live in `event.meta`, not on the brain, and the runtime (which has the
event) is what calls it. Operators wanting custom formatting wrap/replace
this one function rather than subclassing every brain.

Sanitisation: strip control chars, cap at 20 lines (task descriptions are
operator prose; this just guards against a pathological payload bloating
every prompt).

## 7. Failure modes named

1. **Cache write fails** (disk full, permission). `set` returns False;
   the dispatch proceeds — the goal is an optimization, and `meta` still
   carries the task (§5.4). Logged once as a warning.

2. **Cache read fails / corrupt JSON.** `goal_text` returns `""`; no goal
   applied this turn; `meta` fallback covers it (§5.4). Self-heals on the
   next successful `set`.

3. **Stale clear after a new task started on the same conversation.**
   `clear(conversation_id, task_id)` clears only if the stored `task_id`
   matches; a mismatched stale clear is a no-op, preserving the active
   goal. (Same-conversation task replacement is rare — a new task tree
   gets a new `task-root` conversation — but the guard is cheap.)

4. **Gateway restart with a goal set.** No action needed: the cache is a
   file; the next dispatch reads it and applies via the brain's channel
   (§4). There is no brain session to re-issue into (§0.1.1/0.1.2), so
   rev-1's "re-issue set_goal on startup" step is deleted.

5. **Concurrent dispatch across slots.** Writes are single-writer (dispatch
   loop, §3); worker threads only read; `os.replace` is atomic. No
   corruption, no lock needed.

6. **Goal text with newlines/odd tokens.** §6 sanitiser strips control
   chars and caps line count; the goal is plain text in a system prompt or
   prompt body, not parsed as a command, so there is no injection surface
   beyond ordinary prompt content.

7. **Brain backend swap mid-task** (operator changes `default_brain`).
   The cache is brain-agnostic; the new brain applies the same entry via
   *its* class channel (§4). If the swap is system-prompt→body, the body
   class injects on its next fresh session; if body→system-prompt, the env
   append starts immediately. Continuity preserved with no per-brain state.

8. **Body-class re-inject on resume** (codex-exec, opencode, aider). Naively
   prepending the goal every turn would accumulate it in the resumed
   transcript. Guarded by the first-turn-only policy (§4.2): inject when
   `resume_session is None`, skip on resume. System-prompt class is immune
   (ephemeral append, never in transcript).

9. **Task goes terminal.** Depends on §5.3. With (A): `company.task_closed`
   → `clear`, worst case one poll interval (~10 s) of stale goal. Without
   (A), with (B): cleared within the TTL. Without either: **the goal
   leaks** — which is exactly why §5.3 gates the set path.

## 8. Observability

- One log line per `set`: `goal set conv=<id> task=<id> text_chars=<n>`.
- One log line per `clear`: `goal cleared conv=<id> prev_task=<id>`.
- `goal.json` is human-inspectable; `jc-doctor` can surface "conversation
  X goaled on task Y".
- No new metric counter.

## 9. Test plan

- Unit (`goal_cache`): set/get/clear round-trip; clear with mismatched
  task_id is a no-op; missing/corrupt file → `get` returns None; atomic
  replace leaves no torn file.
- Unit (system-prompt class): with a cache entry for
  `event.conversation_id`, `base.invoke` sets `JC_GOAL` in the subprocess
  env; empty/absent → no `JC_GOAL`. Adapter test: `claude.sh`/`pi.sh` add
  `--append-system-prompt "$JC_GOAL"` iff `JC_GOAL` is set.
- Unit (body class): the `<goal>` block is prepended on a fresh session
  (`resume_session is None`) and **omitted on resume** — the accumulation
  guard (§4.2).
- Integration: a `company.task_assigned` (conversation `task-root:R`) →
  the next dispatch applies the goal via the active brain's channel; a
  `company.task_closed` → next dispatch has none. (Requires §5.3 (A) or a
  test stub emitting `task_closed`.)
- Restart: write `goal.json`, restart, assert the next dispatch still
  renders the goal (file-driven, no re-issue).
- Conversation isolation: a chat event (`telegram:*`) interleaved with a
  task conversation renders no goal on the chat turn.
- TTL fallback (if §5.3 (B)): a goal past TTL is dropped on `get`.

## 10. Open questions

- **Q1** (resolved by grounding): Does claude-code preserve `/goal` across
  restarts via its session store? Moot — slash commands are not executed
  in `-p`/headless mode (§0.1.2), so there is no native goal state to
  preserve. The cache file is the source of truth.
- **Q2**: §5.3 — which clear trigger ships first: (A) extend
  company-inbox to emit `company.task_closed`, (B) TTL age-fallback, or
  both? Recommendation: ship (B) as the floor in this work, (A) as the
  precise trigger (small #64 follow-up). Operator/maintainer call.
- **Q3**: Goal text cap — 500 chars enough for the multi-step tasks we
  expect, or make it configurable? Default 500; revisit if real tasks
  truncate badly.
- **Q4**: Persona exposure (PR #3) — rely on the `<goal>` block in the
  prompt, the `meta.kind=task_assigned` payload, or both? Default: both
  (§5.4). Confirmed-belt-and-braces; PR #3 decides what the persona text
  actually references.

## 11. Sequence after this spec lands

This PR is **spec-only**. Implementation order, once §5.3 (Q2) is decided:

1. **Clear-trigger prerequisite** — §5.3 (A) and/or (B). Without a clear
   path, the set path leaks goals; this must land first or together.
2. Implementation PR — `lib/gateway/goal_cache.py` + `format_goal`;
   dispatcher set/clear hooks in `lib/gateway/runtime.py`; `base.invoke`
   sets `JC_GOAL`; `claude.sh`/`pi.sh` append `--append-system-prompt
   "$JC_GOAL"` (system-prompt class, §4.1); body-class `<goal>` block via
   `base.Brain.prompt_for_event` rendered first-turn-only (§4.2); tests.
3. Follow-up spec — persona prompt for `task_assigned` handling
   (companion PR #3 of this trilogy).

Depends on PR #64 (merged): the channel emits `company.task_assigned` with
`conversation_id=task-root:<root_id>` and `meta.kind=task_assigned`, which
are the set trigger and the goal source. The clear trigger
(`company.task_closed`) is **not** provided by #64 as merged — see §5.3.

No backend changes in the-company are required.
