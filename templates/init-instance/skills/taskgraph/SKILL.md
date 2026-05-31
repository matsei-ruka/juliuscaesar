---
name: taskgraph
description: Read, create, spawn, and update Omnisage task graph work in The Company through the local jc command.
---

# Task Graph

Use this skill when you need to inspect, create, assign, or update tasks in The
Company task graph.

All task operations must go through `jc company task ...`. Do not claim a task
was created, spawned, or updated unless the command returned a successful JSON
response with a task `id`.

## Commands

Discover same-company agents and owner slugs:

```bash
jc company agents list
```

Read your inbox:

```bash
jc company task inbox
```

List tasks:

```bash
jc company task list --status pending --status in_progress --limit 50
```

Get task detail:

```bash
jc company task get <task_id>
```

Create a root task for an agent in your company:

```bash
jc company task create \
  --owner <agent_slug> \
  --title "Concrete task title" \
  --description "What needs to happen and why." \
  --payload '{"expected_output":"Specific result required","reason":"Why this task exists"}'
```

Spawn a child task under an existing task:

```bash
jc company task spawn <parent_task_id> \
  --owner <agent_slug> \
  --title "Concrete child task title" \
  --description "The child work item." \
  --payload '{"expected_output":"Specific result required"}'
```

Update task status:

```bash
jc company task update <task_id> --status accepted
jc company task update <task_id> --status in_progress
jc company task update <task_id> --status blocked --result '{"blocker":"Waiting on credentials"}'
jc company task update <task_id> --status done --result '{"summary":"What changed","artifacts":["link-or-path"]}'
jc company task update <task_id> --status failed --result '{"error":"What failed","next_step":"Retry path or owner"}'
```

Add an interim comment without closing the task:

```bash
jc company task comment <task_id> --message "I saw this and am waiting on the publish job."
jc company task comments <task_id>
```

## Policy

- A task must have one owner, one concrete next action, and one expected output.
- Use agent slugs for owners. Do not guess UUIDs.
- If you do not know the exact owner slug, run `jc company agents list` before
  creating or spawning the task.
- Create root tasks for independent work.
- Spawn child tasks when work belongs under an existing root.
- Before creating, check for obvious duplicates with `task inbox`, `task list`,
  or `task get` if a related task ID is known.
- Never mark a task done without a useful `--result` payload.
- Use comments for acknowledgements, interim updates, blockers, and handoff
  notes. Use `--result` only for final deliverables or terminal failure data.
- Never hide failure. If a command fails, report the error and leave the task
  state truthful.

## State Changes

Normal path:

`pending -> accepted -> in_progress -> done`

Failure paths:

`pending -> rejected`

`accepted|in_progress -> blocked`

`accepted|in_progress|blocked -> failed`

Avoid jumping straight from `pending` to `done`. Accept the task first unless
the task is invalid and should be rejected.

## Failure Modes

- Missing `COMPANY_ENDPOINT` or `COMPANY_API_KEY`: task commands cannot run.
- Cross-company owner: backend rejects the create/spawn request.
- Wrong owner slug: backend rejects the task.
- Invalid state transition: backend rejects the update.
- No returned task ID: the task does not exist. Say that plainly.
