---
title: Heartbeat scheduled task runner
section: subsystem
status: active
code_anchors:
  - path: bin/jc-heartbeat
    symbol: "run <task-name>"
  - path: lib/heartbeat/runner.py
    symbol: "def run_task(instance_dir: Path, task_name: str, dry_run: bool = False) -> int:"
  - path: templates/init-instance/heartbeat/tasks.yaml
    symbol: "only_if_delta"
last_verified: 2026-04-25
verified_by: l.mattei
related:
  - contract/adapter-and-delivery-contracts.md
  - contract/config-and-secret-boundaries.md
---

## Summary

Heartbeat is the cron-driven scheduled work system. It reads `<instance>/heartbeat/tasks.yaml`, builds a prompt from L1 memory, optional context files, an optional pre-fetch bundle, and a task prompt, then calls a configured brain adapter. Non-empty output is delivered to Telegram unless `--dry-run` is set or output is empty / `SILENT`.

## Pipeline

1. Resolve instance directory.
2. Load `.env`.
3. Load `heartbeat/tasks.yaml`.
4. Acquire a per-task flock under `heartbeat/state`.
5. Optionally run `pre_fetch` bash script and write a bundle.
6. If `only_if_delta` is true, hash the bundle and skip unchanged runs.
7. Load all L1 memory files.
8. Load configured `context_files`.
9. Render template variables: `bundle_path`, `date`, `time`, `timezone`.
10. Write the final prompt under `heartbeat/state/prompts`.
11. Call adapter from `lib/heartbeat/adapters/<tool>.sh`.
12. Write output under `heartbeat/state/outputs`.
13. Send to Telegram and append to `heartbeat/state/sent.log`.

## Task configuration

Each task can set `tool`, `model`, `folder`, `pre_fetch`, `context_files`, `only_if_delta`, `prompt`, and `destination`. Defaults can be declared under `defaults:`.

Named destinations are optional. If absent, delivery falls back to `TELEGRAM_CHAT_ID` from `.env`.

## Invariants

- `pre_fetch` scripts run under `<instance>/heartbeat`.
- L1 memory is always prepended.
- The adapter contract is stdin prompt to stdout response.
- `SILENT` suppresses delivery.
- Locks prevent overlapping runs of the same task.

## Open questions / known stale

- 2026-04-25: Only Telegram delivery is implemented for destinations.
