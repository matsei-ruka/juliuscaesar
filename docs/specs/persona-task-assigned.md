# Persona Prompt for `task_assigned` Events — Operating Spec

Status: Draft — spec-only PR, no code in this branch
Date: 2026-05-27
Author: Noah
Repo: `juliuscaesar`
Related: `docs/specs/company-inbox-channel.md` (delivery side),
         `docs/specs/goal-integration.md` (the /goal mechanism),
         `docs/specs/persona-system.md` (where this prompt lands),
         the-company `docs/specs/the-company-v2.2-task-graph.md` (FSM rules)

---

## 0. What this spec is, and is not

This spec defines a small prompt fragment that gets injected into every
agent's persona system text. The fragment teaches the persona how to
recognise a task assignment delivered through the `company-inbox`
channel, how to walk the task through the legal state transitions, and
what shape to put in the result payload.

It is **not** a new event type — `company.task_assigned` already exists
and lands in the queue today. It is **not** a new transport. It is the
final wire between "the channel injects an event" and "the brain
produces useful output."

Without this spec, every persona has to rediscover the calling
convention by trial and error. Today's dogfood with Ethan made that
cost concrete (§1).

## 1. Motivation — Ethan's notes from the dogfood

On 2026-05-27 we spawned the first real task through the company-inbox
channel: "Introduce yourself: host, IP, framework version, supervisor
PID." Ethan answered correctly, but his result payload included a
section called `notes_for_pr3` listing every rough edge he had to
solve on the fly:

1. There was no `jc-cli` wrapper for `PATCH /api/tasks/{id}`. He had
   to construct a `curl` by hand, pulling the API key from
   `state/company/api-key` and the agent_id from `ops/the_company.yaml`.
2. His first PATCH attempt went straight from `pending` to `done` and
   was rejected with `illegal_transition`. The task state machine
   requires `pending → accepted → in_progress → done`, but nothing in
   his context told him that. He learned by hitting the 422.
3. `framework_version` is only exposed by `jc company status` as
   free-form text. No JSON-friendly output for a script to consume.
4. The supervisor PID lives in `state/supervisor/jc-supervisor.pid` but
   `jc supervisor status` doesn't print it.
5. The `task_id` he needed for the PATCH was buried inside the gateway
   routing metadata block of the event. Not impossible to find, but
   not a first-class field.
6. The shape of `result.payload` is freeform. He had to guess field
   names from the task description.

Items 1, 3, 4 are CLI improvements that fall outside this spec (each
becomes its own small ticket). Items 2, 5, 6 are persona-level concerns:
the brain needs guidance, and the place for that guidance is the persona
prompt.

## 2. Scope

### 2.1 In scope

- A prompt fragment template at
  `lib/personas/fragments/task_assigned.md.j2` (Jinja, like the
  existing persona fragments).
- Inclusion of that fragment into every persona's compiled system text
  whenever the persona is configured to participate in the task graph
  (see §3).
- A canonical `result.payload` shape (§5).
- A reference to the state machine (§4) the persona must walk.

### 2.2 Out of scope (deferred — separate tickets)

- A `jc task patch` CLI wrapper (item 1). Big enough to be its own
  spec; this fragment assumes the wrapper exists OR documents the
  raw `curl` until it does.
- `jc company status --json` (item 3). Trivial flag addition; one
  follow-up commit, no spec needed.
- `jc supervisor status` printing the PID (item 4). Same — one-liner.
- Restructuring the event payload to make `task_id` first-class
  (item 5). Touches the queue ingestion path; lives in a follow-up.

## 3. Persona opt-in

Not every persona should handle task assignments. A pure dialog persona
(say, Sofia the social-media manager) might never be on the
operational task graph. We don't want the prompt fragment polluting
her system text.

The opt-in lives in `persona.yaml`:

    task_graph:
      participates: true     # default false
      preferred_status_path: accept_then_work   # see §4

When `participates: true`, the persona compiler appends the fragment
to the system text. When false, the fragment is omitted and the persona
is unaware of the convention — the channel may still inject events,
but the brain will respond with a polite "I don't think this was meant
for me" rather than guess. Operator's choice.

For Phase 1 the migration default is **false** for everyone, then
operator-flipped per agent as they're brought onto the task graph
(Sergio, Ethan, Penelope first). Existing personas don't drift.

## 4. The state machine — what the persona must know

The task FSM (defined in the-company `task_auth.py:ALLOWED_TRANSITIONS`)
is the contract the persona is being asked to respect. The fragment
must teach it:

    pending → accepted        (you have read the task and are taking it)
    accepted → in_progress    (you are actually executing now)
    in_progress → done        (work succeeded; result.payload is set)
    in_progress → failed      (you tried, it broke; error in result)
    in_progress → blocked     (you need help; raise an approval)
    any non-terminal → rejected    (you decline the task entirely)
    blocked → in_progress     (the approval came through; resume)

The shortcut `pending → done` is forbidden — that's what bit Ethan.

Two transitions in particular need brain-side judgment:

- **accept vs. reject** at first read. Reject if the task is misrouted
  (wrong owner), doesn't fit your persona's competence, or is
  obviously broken. Accept otherwise — even uncertain. The persona is
  allowed to fail; what it's not allowed to do is silently ignore.
- **done vs. failed**. The distinction is: did you produce the
  requested artifact (even imperfectly)? If yes, `done` with a
  result. If no, `failed` with an error reason.

`blocked` is for a different kind of stuck: the task can't proceed
without a human or sibling-agent decision. The fragment teaches the
persona to use it but doesn't try to encode the approval-raising
mechanism (see `the-company-v2.2-task-graph.md` §6 for that path).

## 5. Canonical `result.payload` shape

The current ORM column `tasks.result` is `JSONB`, free-form. That's the
right backend choice — different task types produce different artifacts.
But personas need a default shape so they don't reinvent field names
every time.

The fragment teaches this:

    {
      "payload": <task-specific body>,
      "notes": "<free-text observation, optional>",
      "warnings": ["<any non-fatal issues the persona wants to flag>"]
    }

For `failed`:

    {
      "error": {
        "code":  "<short stable identifier>",
        "message": "<one-line human-readable>",
        "details": <optional structured>
      },
      "notes": "<context about what was tried>"
    }

Task-specific bodies still live under `payload`. The persona is free
to add extra top-level keys for one-off needs, but `payload` / `error` /
`notes` are the agreed slots and the fragment makes that clear.

## 6. Fragment outline (concrete text — informational, not normative)

The actual prose lives in `lib/personas/fragments/task_assigned.md.j2`.
The shape is roughly:

> When you receive an event with `source = "company-inbox"`, it is a
> task assignment from the-company task graph. The event metadata
> contains:
>
> - `task_id` — the UUID you need to PATCH.
> - `root_id` — the root task this descends from (or equals `task_id` if root).
> - `title`, `description` — what you've been asked to do.
> - `payload` — task-specific input data, may be empty.
>
> Your job in three steps:
>
> 1. **Decide.** Accept the task by PATCHing it from `pending` to
>    `accepted` (one HTTP call). If you can't or won't do it, PATCH it
>    to `rejected` with an explanation in `result.error.message`. Do
>    NOT ignore the event silently — leaving it in `pending` causes the
>    janitor to mark it `expired` later, which is the worst outcome
>    (looks identical to "the system is broken").
>
> 2. **Work.** PATCH `accepted → in_progress` when you start. Do the
>    actual work. If you need to delegate sub-tasks, use `POST
>    /api/tasks/{this_task_id}/spawn` (see the goal-integration spec).
>
> 3. **Close.** PATCH `in_progress → done` with `result.payload =
>    {your artifact}`. If you couldn't finish, `failed` with `result.error
>    = {...}`. The shape is documented in §5 of this prompt fragment.
>
> The FSM does **not** allow shortcuts. You can't go directly from
> `pending` to `done`; you must walk the states. If you try, the
> backend returns 422 `illegal_transition`.

The fragment is short on purpose. Most personas will compose
task_assigned handling with their own competence ("if it's a code
task, I run my code skill; if it's a research task, I run search"). The
fragment owns the *protocol* — the wrapping FSM and payload shape.

## 7. Failure modes named

1. **Persona accepts but never PATCHes to in_progress.** The task sits
   at `accepted` until the janitor expires it. Symptom: status stays
   `accepted` for hours. Diagnose by reading the brain's last output
   for that conversation. Mitigation: persona prompt explicitly says
   "PATCH accepted → in_progress when you start actual work."

2. **Persona PATCHes to `done` with empty payload.** This is the
   "silent the task" anti-pattern. The persona prompt names it:
   *empty `result.payload` with no `error` block is a failure mode, not
   a success.* If the persona genuinely has nothing to return, it
   should `failed` with `error.code = "no_artifact"`.

3. **Persona invokes the wrong task_id.** Operator-fixable: re-spawn
   the task. The persona's PATCH on the wrong UUID returns 404; the
   brain sees the error and can adjust.

4. **Persona doesn't know its own agent_id for sub-spawn.** Handled by
   the companion `agent-self-discovery` spec — by the time the brain
   needs to spawn, the channel has already persisted `COMPANY_AGENT_ID`
   in `.env`. The persona prompt links here.

5. **Task arrives but the persona is mid-conversation with a human.**
   The gateway dispatches one event at a time per conversation slot.
   If the persona is busy, the inbox task waits in the queue. No
   special handling needed in the prompt.

## 8. Test plan

This is prompt content, not code, so the tests are different:

1. **Snapshot test of compiled persona text.** Compile a sample persona
   with `task_graph.participates: true`; assert the fragment text is
   in the output. Compile the same persona with `false`; assert the
   fragment is **not** present.

2. **Worked example.** A fixture task `{title: "Echo back", payload:
   {echo: "ping"}}` injected into a test persona. The fixture expects
   the persona to walk pending → accepted → in_progress → done with
   `result.payload = {echo: "ping"}`. This isn't a unit test of the
   prompt — it's an integration sanity check that the prompt produces
   the right protocol behaviour from a real model run.

3. **Cross-persona consistency.** Compile three personas
   (Sergio, Ethan, Penelope), assert the fragment text is byte-identical
   in all three (it's a static fragment, no persona-specific
   substitution).

## 9. Rollout

1. Land this spec PR.
2. Implementation PR adds the fragment file + the compiler hook.
3. For each agent we want on the task graph, flip
   `task_graph.participates: true` in their `persona.yaml` and
   recompile. Restart their gateway (the compile happens at boot).
4. Run a real task through. Observe the FSM walk in the dashboard.
   If the persona violates the FSM, the operator gets a 422 in logs;
   adjust the fragment text until convergence.

## 10. Open questions

- **Q1 — Do we ship the fragment in English only, or per-persona
  language?** Many personas speak Italian to Luca but reason in English
  internally. The protocol fragment is brain-internal, so English-only
  is probably fine. Defer to first multilingual feedback.

- **Q2 — Where does the `jc task patch` wrapper live?** A new
  `bin/jc-cli` subcommand makes sense. The fragment can reference it
  by name even before it ships, with a fallback `curl` example. When
  the wrapper exists, the fragment is updated to point at it as
  primary.

- **Q3 — Should the fragment teach the persona to spawn children?**
  Sub-task spawning is a real capability of the task graph. For Phase 1
  the fragment stays narrow (accept/work/close). The spawn pattern is
  documented separately in `goal-integration.md` and can be referenced
  from this fragment without inlining.

- **Q4 — Idempotent re-injection.** If the channel re-injects a task
  the persona has already PATCHed (boot dedup misfire), the persona
  re-reads it and… does what? Options: PATCH to the current status
  again (no-op since transitions like `done → done` are illegal), or
  see the current status and exit silently. The fragment should tell
  it to do the latter — "if the task is already in a terminal status
  when you receive it, do nothing and log a note." Worth including
  even though the channel's dedup makes this rare.
