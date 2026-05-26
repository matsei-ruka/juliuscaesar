# Company Inbox Channel — Operating Spec

Status: Design draft (rev 2 — grounded against codebase), spec-only PR
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
- Periodic HTTP poll (default every 10 s) against the agent's own inbox
  endpoint, status filter taken from config (§3). Agent identity and
  credentials are the *same ones the supervisor reporter already uses* —
  loaded via `lib/company/conf.py` into a `CompanyConfig`, reused through
  `CompanyClient` (`lib/company/client.py`). See §3/§7 and Q4.
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
  into one "session". The gateway's existing parallel-slot relatedness
  classifier (`lib/gateway/runtime.py`) handles affinity at dispatch time,
  keyed on `conversation_id`. This channel only has to set `conversation_id`
  correctly on inject (§5) and enqueue; it implements no routing itself.

## 3. Configuration

`ops/gateway.yaml`:

```yaml
channels:
  company-inbox:             # hyphen key, matching `jc-events` in registry.py
    enabled: false           # opt-in per-instance; default off
    poll_interval_seconds: 10
    max_new_per_tick: 5      # cap on tasks accepted in a single poll
    inbox_status_filter:     # which statuses to pull; default below
      - pending
      - accepted             # so the agent can resume after restart
```

Credentials are **not** a separate file. The company endpoint and api_key
already live in the instance `.env` (`COMPANY_ENDPOINT`, `COMPANY_API_KEY`)
and are loaded by `lib/company/conf.py` into a `CompanyConfig` — the exact
object the supervisor reporter (`lib/company/reporter.py`) uses. The channel
reuses that config and the shared `CompanyClient` (`lib/company/client.py`).
Agent identity is whatever `jc company register` enrolled — the same identity
the reporter publishes under (exact field/path shape: see Q4). If the company
block is disabled or `.env` lacks `COMPANY_API_KEY`, the channel logs a single
startup warning and stays inert.

## 4. Lifecycle

```
gateway boot
  → company-inbox channel start (if enabled)
  → load CompanyConfig (endpoint, agent identity, api_key) via lib/company/conf.py
  → enter poll loop
       every poll_interval_seconds:
         GET {endpoint}/api/agents/{self}/inbox
             ?status={inbox_status_filter csv}
             &order=created_at         # oldest first; see §6
             &limit={max_new_per_tick * 2}
         for each task in response.items (created_at ASC):
           if task.id in seen_cache: continue
           inject event into queue.db (see §5)
           seen_cache.add(task.id)
           stop after max_new_per_tick injects this tick
         done
  → on shutdown: drain in-flight HTTP, exit cleanly
```

The `seen_cache` is **boot-scoped** — wiped on gateway restart. Re-seeing a
task on the first post-restart poll is benign: `queue.enqueue()` uses
`INSERT OR IGNORE` against the partial unique index
`idx_events_dedup (source, source_message_id) WHERE source_message_id IS NOT NULL`
(`lib/gateway/queue.py`). A duplicate returns `(event, inserted=False)` and
writes nothing — a true no-op, not an upsert. This holds **as long as the row
survives**: there is no queue.db retention/culling today (§10/Q5), so in the
current codebase rows never vanish and the dedup guarantee is durable.

## 5. Event injection shape

Each new task becomes one row in `queue.db.events` via `queue.enqueue()`:

```
source             'company-inbox'
source_message_id  'task:<task.id>'              # dedup key (idx_events_dedup)
conversation_id    'task-root:<task.root_id>'    # affinity key — see below
status             'queued'                       # schema default; LOCAL event
                                                  #   status, not company status
content            "<task.title>\n\n<task.description>"
meta               JSON:
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

Column names match the real schema (`lib/gateway/queue.py`):
`source_message_id` (not `source_event_id`), and `status` defaults to
`'queued'`.

**Affinity / slot routing.** The gateway already has parallel-slot dispatch
(`lib/gateway/runtime.py`): `claim_batch_same_conversation` coalesces queued
events sharing a `conversation_id`, and a relatedness classifier assigns
slots. Crucially, an event with a **NULL `conversation_id` runs alone on slot
0 with no affinity** (runtime.py: "No conversation_id → no slot affinity to
compute"). So to make sub-tasks of one task tree route/coalesce together, the
channel sets `conversation_id = "task-root:<root_id>"`. Sub-tasks of distinct
roots get distinct conversations and are eligible for separate slots. That is
the *entire* integration with slot routing — set the key, enqueue; no routing
logic in this channel. (There is no `root_id` column in queue.db; `root_id`
lives in `meta` and is encoded into `conversation_id`.)

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

Credentials come from `.env` (`COMPANY_API_KEY`), loaded into `CompanyConfig`
by `lib/company/conf.py` — the same path the supervisor reporter and
`CompanyClient` use (`lib/company/client.py` sets `Authorization: Bearer
<api_key>`). The channel does **not** read a bespoke key file.

```
Authorization: Bearer <api_key>
```

If the backend returns `401`, the channel:

1. Logs `company-inbox auth failure — reloading CompanyConfig from .env`.
2. Reloads `CompanyConfig` (operator may have re-enrolled via
   `jc company register`, which rewrites `COMPANY_API_KEY` in `.env`; there
   is no dedicated rotate-key endpoint today — see Q5).
3. If the reloaded key is unchanged, enters degraded state: poll interval ×
   4, keep retrying. No crash, no event spam.

**Loud-on-auth-failure (mandatory).** A silent degraded mode re-creates the
exact failure §1 exists to kill: the agent quietly does no work and nothing
surfaces it. So a *persistent* 401 (degraded beyond one interval) MUST
escalate visibly — at minimum a `WARN` the health endpoint / supervisor
surfaces, plus a one-shot notice through the existing reporter channel, so
the operator sees "agent X: inbox auth failing" rather than only inferring it
from tasks rotting at `pending`. Degraded ≠ silent.

## 8. Failure modes named

1. **The-company unreachable** (DNS, TLS, 5xx). Backoff per §6; no panic.
   Channel posts a warning to `gateway.log` after the first 3 failures,
   then is silent until success.
2. **api_key revoked** (401 persistent). Channel enters degraded mode
   (§7) and continues retrying so a re-rotation resumes without restart.
3. **Duplicate task injection across restart**. Boot-cache wipe + the partial
   unique index `idx_events_dedup (source, source_message_id)` +
   `INSERT OR IGNORE` make the re-inject a no-op (§4). Durable only while the
   row exists — no retention exists today, so this currently holds (Q5).
4. **Task already terminal by the time we poll**. The inbox endpoint
   filters server-side; if a race makes us pull a task that's flipped to
   `done` between the SELECT and our INSERT, the brain will still get the
   event. It can PATCH `accepted → in_progress → done` idempotently, or
   notice the conflict and PATCH `rejected` with a note. Persona's call.
5. **Tasks owned by a different agent appear**. Should not happen — the
   query is `WHERE owner_agent_id = self`. If it does (backend bug), the
   channel asserts and logs; does not inject.
6. **`jc company register` rewrites `.env` mid-poll**. The channel reloads
   `CompanyConfig` between ticks, not mid-request, so a request always uses
   one coherent key. A request in flight when `.env` changes either completes
   on the old key (next tick reloads) or 401s and triggers the reload path
   (§7). No torn read, and no bespoke key-file atomicity argument is needed:
   the key is config-loaded, not byte-read from a path.
7. **Channel disabled at runtime via gateway.yaml edit + SIGHUP**. The
   channel acknowledges the disable, drains the seen_cache (so a future
   re-enable starts fresh), exits its poll loop. Other channels unaffected.
8. **Long backend pause + flood on resume**. If the backend goes down
   for 1 h and comes back with 50 pending tasks for this agent, the
   `max_new_per_tick: 5` cap stretches the resync over 10 ticks ≈ 100 s.
   No thundering herd.

## 9. Observability

- `gateway.log` emits one line per poll cycle at INFO level only when new
  tasks were injected. Format:
  `company-inbox tick injected=2 task_ids=[abc, def] cache_size=14`
- A `company.inbox.tick` counter is incremented alongside existing gateway
  runtime counters.
- Degradation is **not** invisible: per §7 a persistent 401 or prolonged
  unreachable backend escalates to a health-visible WARN plus a reporter
  notice. Normal operation is otherwise quiet — its success shows as tasks
  getting processed instead of rotting at `pending`. The dashboard already
  shows the tasks themselves; nothing new is visualised for the happy path.

## 10. Test plan (for the implementation PR, not for this spec)

- Unit: poll loop with a stub backend, dedup cache, 429 backoff curve.
- Integration: against a real the-company dev instance, assign 3 tasks,
  verify all 3 land in queue.db within 1 poll interval.
- Failure injection: kill the-company mid-poll, verify graceful backoff;
  rotate the api_key via the-company API, verify the channel picks up
  the new key on the next read.
- Resource: 100 polls × 5 tasks each. NOTE: there is **no** queue.db
  retention/culling today (`lib/gateway/queue.py` only re-queues expired
  leases via `requeue_expired`, never deletes), so injected rows accumulate.
  Good for dedup durability (§4) but means unbounded growth is a real,
  currently unowned concern — see Q5. The test should *measure* growth to
  feed the retention decision, not assume a policy that does not exist.

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
- **Q4**: Agent identity for the inbox path. §1 speaks of an owner *slug*
  (`sergio_dev_ops`) while `lib/company/conf.py:instance_id` is a sha256 of
  the instance path. The inbox query must key on whatever `owner_agent_id`
  the-company assigns at `jc company register`. Confirm the exact field and
  path shape (`/api/agents/{slug}/inbox` vs token-derived `/api/agents/me/inbox`)
  against the-company before implementation. The channel must reuse the
  reporter's established identity, not invent one.
- **Q5**: Two adjacent gaps surfaced while grounding this spec against the
  code. (a) **No api_key rotation** endpoint/command exists — only
  `jc company register` re-enrollment. Sufficient, or do we want a dedicated
  rotate path? (b) **No queue.db retention** — injected task events accumulate
  forever (`queue.py` never deletes). Who owns culling, and on what key (age?
  terminal company-status confirmed via a follow-up GET)? Culling interacts
  with dedup (§4): deleting the row for a still-`accepted` task lets it
  re-inject after a reboot, so retention and the `accepted` resync filter (Q1)
  must be designed together.

## 12. Sequence after this spec lands

This PR is **specs-only**. Once reviewed:

1. Implementation PR — `lib/gateway/channels/company_inbox.py`, runtime
   wiring in `lib/gateway/runtime.py`, config validator updates, tests.
2. Follow-up spec — `/goal` integration (companion PR #2 of this
   trilogy).
3. Follow-up spec — persona prompt for `task_assigned` event handling
   (companion PR #3).

No backend changes in the-company are required for any of this.
