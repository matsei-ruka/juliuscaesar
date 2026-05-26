# Goal Integration — Brain-Agnostic /goal Anchor

Status: Design draft, spec-only PR
Date: 2026-05-26
Author: Noah
Repo: `juliuscaesar`
Related:
- `docs/specs/company-inbox-channel.md` (PR #64 — companion #1 of the trilogy)
- `the-company` v2.2 task graph (`docs/specs/the-company-v2.2-task-graph.md` in matsei-ruka/the-company)

---

## 0. What this spec is, and is not

This spec defines a **brain-agnostic abstraction** for the `/goal`
feature recently shipped on `claude-code` and `codex`, and lays out how
to provide equivalent semantics on the other brain backends the
framework supports (`pi`, `opencode`, `aider`, `gemini`, `openrouter`).

It is **not** a redesign of the brain adapter layer. It adds two
methods (`set_goal`, `clear_goal`) to the base `Brain` class and
wires them into the task lifecycle.

## 1. Motivation

`/goal` anchors the model on a high-level objective that survives
across many turns within one session, reducing drift and re-prompt
overhead. With the v2.2 task graph live, the natural mapping is:

> the agent's current goal = the task they are actively executing

Without `/goal`, the brain has to be told "you are working on task X"
in every prompt. Each dispatch repeats the framing, burning tokens and
risking the model losing focus on multi-step tasks. With `/goal`, the
framing is set once when the task is accepted and survives until the
task moves to a terminal state.

Failure mode of record without this spec: an agent that took 6
dispatches to finish a multi-step task pays the cost of re-explaining
the task in each call, and the longer the conversation grows the more
likely the model is to wander into the chat context's general topic
instead of the task-specific one.

## 2. Scope

### 2.1 In scope

- Two new methods on `lib/gateway/brains/base.py:Brain`:
  `set_goal(text)`, `clear_goal()`. Both return `bool` (success).
- Per-brain implementation, native-first with documented fallback:
  - `claude.py` — `/goal` slash-command
  - `codex.py`, `codex_api.py` — `/goal` slash-command
  - `pi.py` — `/goal` slash-command if exposed by the pi CLI at the
    version we pin; otherwise system-prompt prepend fallback
  - `opencode.py` — system-prompt prepend (no native /goal yet)
  - `aider.py` — `/objective` map (aider's nearest concept); fallback
    to prepend
  - `gemini.py`, `openrouter.py` — API brains, no slash commands;
    system-prompt prepend
- A single dispatcher hook in `lib/gateway/runtime.py` that calls
  `set_goal` when a `company.task_assigned` event begins dispatch and
  `clear_goal` when the underlying task transitions to a terminal
  status (observed via the next inbox poll or via PATCH).
- A goal-cache file at `<instance>/state/gateway/goal.json` so a
  gateway restart can rehydrate the goal of in-flight tasks without
  re-reading the brain's CLI session.

### 2.2 Out of scope (deferred)

- **Multi-goal hierarchies** — a parent task with active children
  whose goals are different. Spec keeps one goal per slot at a time;
  child spawn replaces the current slot's goal until the child closes.
- **Cross-slot goal coordination** — `slot 0` chat vs `slot 1` task
  goals are independent and never sync. Each slot owns its own goal
  state.
- **Goal expiry / time-decay** — the goal stays set until the task
  closes or the gateway sees a contradicting signal.
- **Custom goal templating** — operators wanting their own goal-text
  recipe (e.g. "always include X in the goal") should override
  `Brain.format_goal(task)` in their own subclass. Default
  implementation is `"<title>\n\n<description>"` capped at 500 chars.
- **Native UX in pi.dev / opencode** — adding `/goal` upstream is not
  this PR's concern. We compensate at the framework layer.

## 3. Brain interface change

```python
# lib/gateway/brains/base.py

class Brain:
    ...

    def supports_native_goal(self) -> bool:
        """Does this brain expose a /goal-equivalent slash command?

        Subclasses override. Default False (system-prompt fallback).
        """
        return False

    def set_goal(self, *, text: str, task_id: str) -> bool:
        """Anchor the brain on `text` for subsequent dispatches.

        Implementation strategy depends on the brain. Returns True on
        success, False on any failure — caller falls back to
        in-prompt anchoring (one-shot, not persistent).

        Idempotent: calling with the same `task_id` twice is a no-op.
        Calling with a different `task_id` while a goal is already
        active replaces it (logging the swap).
        """
        ...  # default: write to goal-cache + render in next prompt

    def clear_goal(self, *, task_id: str | None = None) -> bool:
        """Drop the current goal. If `task_id` is provided, clear only
        if the active goal matches (defensive against stale lifecycle
        events arriving out of order)."""
        ...
```

### 3.1 Goal-cache file

Path: `<instance>/state/gateway/goal.json`
Shape:
```json
{
  "slot_0": null,
  "slot_1": {
    "task_id": "64244b95-…",
    "text": "Onboard Francesco Datini\n\nRun the prepared script …",
    "set_at": "2026-05-26T07:00:00Z",
    "native": true
  }
}
```

Updated atomically (tempfile + `os.replace`) on every set/clear.
Survives gateway restarts. On startup, the runtime reads it and
issues a fresh `set_goal` call to the brain CLI for each non-null
slot — re-anchoring through whatever channel the brain provides.

## 4. Per-brain implementations

### 4.1 `claude.py` (Claude Code)

Native. The CLI accepts `/goal <text>` as the first line of a session
turn. Implementation:

```python
def supports_native_goal(self): return True

def set_goal(self, *, text, task_id):
    # prepend "/goal <text>" to the next adapter call's stdin
    self._pending_slash = f"/goal {text}\n"
    self._write_goal_cache(task_id, text, native=True)
    return True

def clear_goal(self, *, task_id=None):
    self._pending_slash = "/goal-clear\n"
    self._clear_goal_cache()
    return True
```

The `_pending_slash` buffer is consumed by `prompt_for_event` and
prepended to whatever the user-message build produces.

### 4.2 `codex.py` and `codex_api.py` (OpenAI Codex)

Native. Same `/goal` slash-command shape as claude-code. Same
implementation as 4.1 with `prompt_for_event` honouring
`_pending_slash`.

For `codex_api.py` (HTTP, not subprocess CLI): the slash command is
encoded as the first message in the conversation thread with
`role: "system"` and `content: "GOAL: <text>"`. Re-sent on each
request as part of the persistent system block.

### 4.3 `pi.py` (DeepSeek pi-coding-agent)

Native if available, fallback if not. Pi 0.74.0 (current pinned
version per inventory snapshots from May 22) does not expose `/goal`
as a slash command. Fallback path:

```python
def supports_native_goal(self): return False  # until upstream lands

def set_goal(self, *, text, task_id):
    # Stash in goal-cache; prompt_for_event will read and prepend.
    self._write_goal_cache(task_id, text, native=False)
    return True
```

`prompt_for_event` then renders a `<goal>...</goal>` block at the top
of the system context (in the existing `render_preamble` chain), so
every adapter call carries the goal alongside the standard preamble.
This is equivalent in effect to `/goal` but pays the token cost on
every turn — that's the price of no native support.

When upstream pi-dev ships `/goal`, flip `supports_native_goal` to
`True` and route through `_pending_slash` as in 4.1.

### 4.4 `opencode.py`

Same as 4.3 — no native /goal. System-prompt-prepend fallback. The
existing preamble renderer already supports custom blocks; we add a
`<goal>` block keyed off the goal-cache.

### 4.5 `aider.py`

Aider's `/objective` slash sets a persistent task statement; it's the
closest neighbour to `/goal`. Map directly:

```python
def supports_native_goal(self): return True

def set_goal(self, *, text, task_id):
    self._pending_slash = f"/objective {text}\n"
    ...
```

`/objective-clear` for the clear path. Verify version compatibility
at adapter init time; if the version doesn't expose `/objective`,
fall back to system-prompt-prepend.

### 4.6 `gemini.py`, `openrouter.py`

Pure HTTP brains, no native CLI. System-prompt-prepend fallback,
implemented identically to `pi.py`. Goal is part of the `system` role
message on every request.

## 5. Task lifecycle integration

The framework, not the brain, decides when to set/clear:

```
event arrives via company-inbox channel (event_type=company.task_assigned)
  → dispatcher classifies to slot N
  → dispatcher calls slot N's brain.set_goal(text=format_goal(task), task_id=task.id)
  → adapter call (first turn — /goal is in the prompt)
  → result returned, persona may now take action

subsequent dispatch on same task_id (multi-turn task)
  → set_goal is idempotent — no-op or re-render
  → adapter call (next turn — goal is still applied)

task transitions to terminal (done | failed | rejected | cancelled | expired)
  → company-inbox channel observes the change on its next poll
     (the backend's task_events table provides the audit trail)
  → channel emits a synthetic event_type=company.task_closed
  → runtime calls brain.clear_goal(task_id=task.id)
  → goal-cache updated, brain knows it's free
```

### 5.1 Where the lifecycle hooks live

- `lib/gateway/runtime.py` — dispatcher receives the event, looks
  up the slot's brain, calls `set_goal` before invoking adapter.
- `lib/gateway/channels/company_inbox.py` (spec'd in PR #1) — emits
  `company.task_closed` synthetic events when polling notices a
  status flip to terminal. The dispatcher routes these to the
  matching slot and calls `clear_goal`.

### 5.2 Multi-slot semantics

Each slot has its own goal state. `slot 0` may be in the middle of a
Telegram chat with no goal set; `slot 1` may be working on a task
with goal active. The goal-cache file (§3.1) is keyed by slot id.

If the affinity classifier dispatches a related-event to a slot that
already has a different active goal (e.g. user asks a chat question
to the task-slot mid-task), the dispatcher logs the conflict and
**does not** swap the goal — chat events arriving in a task slot are
processed without the goal being touched. The persona may notice the
mismatch and reply accordingly.

## 6. Format of the goal text

Default `Brain.format_goal(task)`:

```
<task.title>

<task.description>
```

Capped at 500 characters. If task.description is longer, it is
truncated at 500 and the full body remains available in the event
`payload`. Operators wanting different formatting subclass
`format_goal` in their own `lib/gateway/brains/<brand>.py`.

## 7. Failure modes named

1. **Brain CLI rejects /goal** (unknown command, version mismatch).
   `set_goal` returns False; the runtime logs the failure and falls
   back to one-shot in-prompt anchoring for this dispatch (passes
   the goal text as a system message rather than a persistent
   anchor). Next dispatch attempts native again — no permanent
   degradation.

2. **Goal-cache write fails** (disk full, permission). `set_goal`
   returns False; runtime continues with one-shot anchoring. Logged
   as a hardware-class warning.

3. **Stale clear arrives after a new task started**. The new task's
   `set_goal` will have already overwritten the cache; the stale
   `clear_goal(task_id=X)` checks the cached task_id matches `X`
   and refuses if not. The active goal is preserved.

4. **Gateway restart with in-flight goal**. On startup, runtime
   reads the goal-cache and re-issues `set_goal` to each brain. If
   the brain CLI lost session state (e.g. claude-code's
   process_sessions.json gc'd), the re-issue is best-effort; the
   first post-restart dispatch may run without the native goal but
   includes the goal-text in the prompt as a one-shot.

5. **Concurrent dispatch on multi-slot**. The goal-cache file is
   updated atomically per-slot; one slot's set_goal does not corrupt
   another slot's entry. Per-slot file locks (POSIX advisory) gate
   writes.

6. **Goal text contains newlines or special tokens that confuse the
   brain's input parser**. Sanitiser strips control chars and caps
   line count at 20. Rare in practice (task descriptions are
   operator-written prose).

7. **Brain backend swap mid-task** (operator changes
   `default_brain` from `claude` to `pi` while a task is in flight).
   Goal-cache is brain-agnostic; new brain reads its task_id +
   text, calls its own `set_goal`. If new brain has no native goal,
   it falls back to prepend. Continuity preserved.

8. **The-company task expires while goal still set**. The deadline
   janitor flips the task to `expired`; company-inbox channel
   observes on its next poll and emits `company.task_closed`. The
   runtime calls `clear_goal`. Worst case: 10 s of stale goal
   between expiry and clear. Acceptable.

## 8. Observability

- One log line per `set_goal` call: `goal set slot=N task=<id>
  brain=<name> native=<bool> text_chars=<int>`.
- One log line per `clear_goal`: `goal cleared slot=N
  prev_task=<id> brain=<name>`.
- `goal.json` is inspectable by humans, useful for `jc-doctor` to
  surface "agent X currently goaled on task Y".
- No new metric counter — the existing `dispatch_ok` already covers
  dispatch volume.

## 9. Test plan (for the implementation PR, not for this spec)

- Unit: each brain's `set_goal` / `clear_goal` in isolation against
  a fake CLI / fake API.
- Unit: goal-cache atomic write under simulated power-cut (fsync +
  rename invariants).
- Integration: ship a `company.task_assigned` through the dispatcher,
  verify the next adapter call includes the goal (native or
  prepended); flip status to done, verify clear fires.
- Integration: gateway restart mid-task, verify goal rehydrated.
- Multi-slot: two simultaneous tasks across slot 0 and slot 1, verify
  no cross-talk.
- Fallback path: force `supports_native_goal` to False on claude,
  verify system-prompt-prepend works equivalently.

## 10. Open questions

- **Q1**: When the brain CLI is `claude-code` and the gateway
  restarts, is `/goal` state preserved in the CLI's own session
  store, or do we need to always re-issue? My read of the
  claude-code docs says it's preserved per session id, and we
  already persist session ids across restarts. To be confirmed in
  the impl PR.
- **Q2**: Should `aider.py` `/objective` be used at all, or fall
  back to system-prompt-prepend even when aider supports it? The
  semantics differ slightly (objective is more imperative than
  goal). Operator opinion welcome.
- **Q3**: For codex_api.py (HTTP brain), should the goal go in the
  `system` block or as a separate `developer` role message (newer
  OpenAI API)? Both work; `system` is more universal.
- **Q4**: Persona prompts in PR #3 will reference "the goal" — do we
  expose it to the persona as a structured field in the event
  metadata, or rely entirely on the brain CLI to surface it? Default
  in §5: the goal is in the metadata `kind=task_assigned` event
  payload AND in the brain's /goal slot. Belt + braces.

## 11. Sequence after this spec lands

This PR is **specs-only**. Once reviewed and decisions on Q1–Q4 are
made:

1. Implementation PR — extend `lib/gateway/brains/base.py`, ship per-brain
   overrides, wire dispatcher hooks, add goal-cache module, tests.
2. Follow-up spec — persona prompt for `task_assigned` event
   handling (companion PR #3 of this trilogy).

The implementation depends on PR #1 (company-inbox channel) being
merged — without that channel emitting `company.task_assigned` and
`company.task_closed`, this trigger surface does not exist.

No backend changes in the-company are required for any of this.
