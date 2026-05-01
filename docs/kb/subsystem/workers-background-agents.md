---
title: On-demand background workers
section: subsystem
status: active
code_anchors:
  - path: bin/jc-workers
    symbol: "def cmd_spawn(args: argparse.Namespace) -> int:"
  - path: bin/jc-workers
    symbol: "def cmd_run(args: argparse.Namespace) -> int:"
  - path: lib/workers/db.py
    symbol: "CREATE TABLE IF NOT EXISTS workers"
  - path: docs/specs/workers.md
    symbol: "Heartbeat is cron-driven scheduled tasks; workers are user-triggered on-demand tasks."
last_verified: 2026-05-01
verified_by: l.mattei
related:
  - contract/adapter-and-delivery-contracts.md
  - subsystem/heartbeat-runner.md
---

## Summary

Workers are on-demand background agents for tasks that should not block the live chat session. They reuse the heartbeat adapter layer but have their own lifecycle, state database, prompt/log/result files, and completion notifications.

Heartbeat is scheduled. Workers are user-triggered.

## Lifecycle

`jc workers spawn` validates the adapter, reads a prompt from stdin or an instance-local prompt file, creates a DB row, writes `state/workers/<id>/prompt`, then double-forks a detached runner. The parent returns immediately with the worker id and pid.

The detached `_run` command marks the worker running, executes the adapter with prompt on stdin, writes stdout to `result`, stderr to `log`, and marks a terminal status.

Terminal states are `done`, `failed`, `cancelled`, and `need_input`.

## Storage

- SQLite DB: `<instance>/state/workers.db`
- Per-worker files: `<instance>/state/workers/<id>/`
- Prompt: `prompt`
- Log: `log`
- Result: `result`
- Debug metadata: `meta.json`

## Named workers

Named workers add `name`, `tags`, and `session_id` columns. When `--name` is supplied without `--fresh`, the latest terminal run with a captured `session_id` can be resumed by setting `JC_RESUME_SESSION` for the adapter, with `WORKER_RESUME_SESSION` retained for compatibility.

Session capture is best-effort and brain-specific:

- Claude scans `~/.claude/projects/<instance-slug>/*.jsonl`.
- Gemini parses `gemini --list-sessions`.
- Codex scans `~/.codex` session stores.
- OpenCode parses `opencode session list --format json`.

## Invariants

- Prompt file paths passed to `spawn` must be inside the instance directory.
- Workers run detached from the spawning shell.
- Notifications enqueue gateway delivery events when `ops/gateway.yaml` exists; older instances fall back to the shared `send_telegram.sh` helper.
- Adapter failures are recorded in the DB and last stderr line where possible.
- Adapter subprocess env includes `JC_IN_WORKER=1` and `JC_WORKER_ID=<id>` (commit 1e8661c) so brains can refuse `jc workers spawn` calls from inside a worker — the recursion would otherwise pin a queue lease while the parent runs unbounded.

## Open questions / known stale

- 2026-04-25: Spec mentions `history`, `gc`, and deeper lifecycle features; current code includes core spawn/run/list/show/tail plus named-worker support, but not every spec item is confirmed here.
