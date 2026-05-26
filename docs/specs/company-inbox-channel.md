# Company Inbox Channel — Operating Spec

Status: Design draft, spec-only PR
Date: 2026-05-26
Author: Noah
Repo: `juliuscaesar`
Related: `the-company` v2.2 task graph (`docs/specs/the-company-v2.2-task-graph.md` in matsei-ruka/the-company)

---

## 0. What this spec is, and is not

This spec defines a new gateway channel — `company-inbox` — that pulls task
assignments from the-company HTTP API and surfaces them to the local brain
as ordinary gateway events. It is the agent-side counterpart of the
already-deployed task graph backend.

It is **not** a generic message bus. It is not push (WebSocket). It is not
the inter-agent messaging substrate described in the older
`inter-agent-protocol.md` — that doc is now superseded by the task graph.

## 1. Motivation

The-company stores tasks. Each task has an `owner_agent_id` and a `status`.
When a PMO (or a human, or a sibling agent) spawns a task with the owner
slug `sergio_dev_ops`, the row lands in Postgres and the dashboard sees it.
**Sergio's gateway does not.** The current architecture has no mechanism
for the agent to learn that work has been assigned to it.

Failure mode of record: tasks sit at `status=pending` forever; the agent
keeps doing nothing while the dashboard says "owner: Sergio, age: 3h".
This is the exact gap the task graph spec called out under §11.4 of the
backend spec, and the spec deferred the fix to the framework.

This is the framework-side fix.

## 2. Scope

### 2.1 In scope

- A new gateway channel named `company-inbox`, configured in
  `ops/gateway.yaml`, parallel to `telegram`, `email`, `jc-events`, `cron`.
- Periodic HTTP poll (default every 10 s) against
  `GET /api/agents/{self}/inbox?status=pending`, scoped to the agent's own
  id resolved via `lib/company/conf.py:instance_id`.
- A boot-scoped in-memory cache keyed by `task_id` to dedup the same task
  across consecutive polls.
- Synthetic event injection into the local `queue.db`, event type
  `company.task_assigned`, with the task body inlined in the event
  `content` and metadata sufficient for the brain to act on it.
- Failure-mode handling: transient HTTP errors, 401 after an api_key
  rotation, missing config, gateway restart with in-flight tasks.
- Config knobs: enable/disable, poll interval, max per-tick burst.

### 2.2 Out of scope (deferred)

- **Push via WebSocket** — sub-second latency variant. The current `pull`
  design is acceptable while the fleet is small and a poll interval of 10 s
  is invisible at human cadence. WebSocket lives in a follow-up spec when
  it becomes a felt problem.
- **/goal integration** — set/clear the brain's `/goal` flag based on the
  current task. Spec'd separately (PR #2 of this trilogy).
- **Persona-level prompt** describing how an agent should respond to a
  `company.task_assigned` event. Spec'd separately (PR #3 of this trilogy).
- **Agent-side response actions** (PATCH /api/tasks/{id}, spawn child,
  raise approval). The brain already has the HTTP skill; we do not codify
  the response surface here.
- **Cross-task affinity** — grouping multiple sub-tasks of the same root
  into one "session". The parallel-slots classifier handles affinity at
  dispatch time; this channel just enqueues.

## 3. Configuration

`ops/gateway.yaml`:

```yaml
channels:
  company_inbox:
    enabled: false           # opt-in per-instance; default off
    poll_interval_seconds: 10
    max_new_per_tick: 5      # cap on tasks accepted in a single poll
    inbox_status_filter:     # which statuses to pull; default below
      - pending
      - accepted             # so the agent can resume after restart
```

`ops/the_company.yaml` (already exists for the supervisor reporter) is
reused for credentials. The channel reads:

- `api_url`
- `agent_id`
- `api_key_file`

Same file, same load semantics. If `the_company.yaml` is missing or
disabled, the channel logs a single startup warning and stays inert.

## 4. Lifecycle

```
gateway boot
  → company-inbox channel start (if enabled)
  → load (api_url, agent_id, api_key) from ops/the_company.yaml
  → enter poll loop
       every poll_interval_seconds:
         GET {api_url}/api/agents/{agent_id}/inbox?status=pending,accepted&limit={max_new_per_tick*2}
         for each task in response.items:
           if task.id in seen_cache: continue
           inject event into queue.db (see §5)
           seen_cache.add(task.id)
         done
  → on shutdown: drain in-flight HTTP, exit cleanly
```

The `seen_cache` is **boot-scoped** — wiped on gateway restart. Re-seeing
a task on the first post-restart poll is benign: the queue.db has a
UNIQUE constraint on `(source, source_event_id)` and the second
injection is a no-op upsert.

## 5. Event injection shape

Each new task becomes one row in `queue.db.events`:

```
source            'company-inbox'
source_event_id   'task:<task.id>'
status            'pending'           (local event status; not task status)
started_at        null
content           "<task.title>\n\n<task.description>"
meta              JSON:
  {
    "task_id":      <task.id>,
    "root_id":      <task.root_id>,
    "parent_id":    <task.parent_id|null>,
    "company_id":   <task.company_id>,
    "created_by":   <task.created_by>,   // {kind: "agent"|"user", id: ...}
    "deadline_at":  <task.deadline_at iso>,
    "max_nodes":    <int>,
    "max_depth":    <int>,
    "max_age_secs": <int>,
    "payload":      <task.payload>,      // pass-through JSON
    "kind":         "task_assigned"
  }
```

The brain receives this through the normal dispatch path. Affinity
classifier sees a fresh `(source, root_id)` pair → routes to a free slot
(or queues behind related work).

The `kind` field is the contract the persona-level prompt depends on.

## 6. Polling cadence and rate

- Default `poll_interval_seconds: 10`. For a fleet of 20 agents, this is
  ≈ 120 req/min on the-company `/api/agents/{id}/inbox` endpoint.
- The endpoint is a single indexed query (`(owner_agent_id, status)` index
  shipped in 20260523_1300_task_graph). At fleet sizes < 200 this is well
  below the backend's measured headroom.
- Per-tick cap `max_new_per_tick: 5` prevents a flooded inbox from
  injecting hundreds of events in one second. Excess tasks are deferred to
  the next tick and surface in order of `created_at ASC`.
- The channel honours HTTP `Retry-After` on 429. Default backoff: double
  the configured interval, cap at 5 minutes.

## 7. Authentication

Reuses the api_key stored in `<instance>/state/company/api-key`. Same
file the supervisor reporter uses. The channel reads the bytes at start
and on `SIGHUP` (config reload). Bearer token shape:

```
Authorization: Bearer <api_key>
```

If the backend returns `401`, the channel:

1. Logs `company-inbox auth failure — clearing cache, re-reading key`.
2. Re-reads the api_key file (the operator may have rotated it via
   `POST /api/agents/{id}/rotate-key`).
3. If the re-read key is identical to the prior value, enters degraded
   state: poll interval × 4, keep retrying. No crash, no event spam.

## 8. Failure modes named

1. **The-company unreachable** (DNS, TLS, 5xx). Backoff per §6; no panic.
   Channel posts a warning to `gateway.log` after the first 3 failures,
   then is silent until success.
2. **api_key revoked** (401 persistent). Channel enters degraded mode
   (§7) and continues retrying so a re-rotation resumes without restart.
3. **Duplicate task injection across restart**. Boot-cache wipe + UNIQUE
   constraint on `(source, source_event_id)` make this a no-op.
4. **Task already terminal by the time we poll**. The inbox endpoint
   filters server-side; if a race makes us pull a task that's flipped to
   `done` between the SELECT and our INSERT, the brain will still get the
   event. It can PATCH `accepted → in_progress → done` idempotently, or
   notice the conflict and PATCH `rejected` with a note. Persona's call.
5. **Tasks owned by a different agent appear**. Should not happen — the
   query is `WHERE owner_agent_id = self`. If it does (backend bug), the
   channel asserts and logs; does not inject.
6. **Polling overlaps with `jc-company register` rotating the api_key
   file mid-read**. The file write is atomic (`O_WRONLY|O_CREAT|O_TRUNC`
   via `os.open` followed by `replace`) — read sees either old or new,
   never a torn middle.
7. **Channel disabled at runtime via gateway.yaml edit + SIGHUP**. The
   channel acknowledges the disable, drains the seen_cache (so a future
   re-enable starts fresh), exits its poll loop. Other channels unaffected.
8. **Long backend pause + flood on resume**. If the backend goes down
   for 1 h and comes back with 50 pending tasks for this agent, the
   `max_new_per_tick: 5` cap stretches the resync over 10 ticks ≈ 100 s.
   No thundering herd.

## 9. Observability

- `gateway.log` emits one line per poll cycle at INFO level only when
  new tasks were injected. Format:
  `company-inbox tick injected=2 task_ids=[abc, def] cache_size=14`
- A `company.inbox.tick` metric is incremented in the gateway runtime
  counters (where parallel-slot dispatch counters already live).
- The dashboard already shows tasks; nothing new visualised here. This
  channel is invisible from the operator UI by design — its existence is
  only visible by *something* working (tasks getting processed instead
  of rotting at `pending`).

## 10. Test plan (for the implementation PR, not for this spec)

- Unit: poll loop with a stub backend, dedup cache, 429 backoff curve.
- Integration: against a real the-company dev instance, assign 3 tasks,
  verify all 3 land in queue.db within 1 poll interval.
- Failure injection: kill the-company mid-poll, verify graceful backoff;
  rotate the api_key via the-company API, verify the channel picks up
  the new key on the next read.
- Resource: 100 polls × 5 tasks each, confirm `queue.db` does not grow
  unboundedly (terminal tasks should be culled by retention policy
  outside this channel's scope).

## 11. Open questions

These are flagged for review, not pre-decided:

- **Q1**: Should the channel pull tasks in *any* non-terminal status
  (pending, accepted, in_progress, blocked) for resync on boot, or only
  `pending`? Trade-off: pulling more lets the brain "remember" what it
  was doing before a crash; pulling less keeps the inject path narrow.
  Default in §3 is `pending, accepted`. Operator opinion welcome.
- **Q2**: Per-task `accept` lease? On inject, optionally PATCH the task
  to `status=accepted` so other code paths know it's been claimed. Adds
  one HTTP call per injection but tightens the lifecycle. Default in
  this spec: **no** — let the persona decide whether to accept.
- **Q3**: Should `cron` and `jc-events` events also be classified as
  `task_assigned` if they reference a task_id, for uniformity? Or stay
  in their native event types? Default: stay native. Task graph events
  only come through `company-inbox`.

## 12. Sequence after this spec lands

This PR is **specs-only**. Once reviewed:

1. Implementation PR — `lib/gateway/channels/company_inbox.py`, runtime
   wiring in `lib/gateway/runtime.py`, config validator updates, tests.
2. Follow-up spec — `/goal` integration (companion PR #2 of this
   trilogy).
3. Follow-up spec — persona prompt for `task_assigned` event handling
   (companion PR #3).

No backend changes in the-company are required for any of this.
