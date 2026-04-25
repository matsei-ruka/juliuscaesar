---
title: Brain capability matrix
section: contract
status: active
code_anchors:
  - path: lib/gateway/brains/dispatch.py
    symbol: "_BRAIN_REGISTRY"
  - path: lib/heartbeat/adapters/claude.sh
    symbol: "exec claude"
  - path: lib/heartbeat/adapters/aider.sh
    symbol: "exec aider"
last_verified: 2026-04-25
verified_by: claude
related:
  - contract/adapter-and-delivery-contracts.md
  - subsystem/gateway-queue.md
---

## Summary

The gateway supports five brains, each invoked via the shell adapter at
`lib/heartbeat/adapters/<name>.sh`. The triage layer consults this matrix
when picking a brain — for example a vision-bearing image event must not be
routed to a brain without vision support.

## Matrix

| Brain    | Tools         | Vision  | File edits | Web | Resume mechanism                        |
|----------|---------------|---------|-----------:|-----|------------------------------------------|
| claude   | yes           | yes     | yes        | yes | `--resume <uuid>`                        |
| codex    | yes           | partial | yes        | no  | `codex exec resume <uuid>`               |
| gemini   | partial       | yes     | partial    | yes | `gemini --resume <uuid \| latest>`       |
| opencode | yes           | no      | yes        | no  | `opencode run --session <id>`            |
| aider    | yes (git ops) | no      | yes        | no  | history file in `AIDER_HISTORY_DIR/<id>` |

## Invariants

- Each brain reads its prompt from stdin and writes the final answer to stdout.
- Stderr is captured into the gateway log.
- `JC_RESUME_SESSION` is the canonical resume env var; `WORKER_RESUME_SESSION`
  is honored as a fallback.
- The `aider` adapter requires `AIDER_HISTORY_DIR` and treats absence of
  `JC_RESUME_SESSION` as a fresh session.

## Open questions / known stale

- 2026-04-25: The `vision` and `web` columns reflect the upstream CLIs as
  of late April 2026. Re-verify when bumping a CLI version.
