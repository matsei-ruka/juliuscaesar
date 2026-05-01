---
title: Brain capability matrix
section: contract
status: active
code_anchors:
  - path: lib/gateway/brains/dispatch.py
    symbol: "_BRAIN_REGISTRY"
  - path: lib/gateway/brains/codex_api.py
    symbol: "class CodexApiBrain"
  - path: lib/heartbeat/adapters/claude.sh
    symbol: "exec claude"
  - path: lib/heartbeat/adapters/aider.sh
    symbol: "exec aider"
last_verified: 2026-05-01
verified_by: l.mattei
related:
  - contract/adapter-and-delivery-contracts.md
  - subsystem/gateway-queue.md
---

## Summary

The gateway supports six brains. Five shell out via `lib/heartbeat/adapters/<name>.sh`; one (`codex_api`) calls the OpenAI Responses API directly via the local Codex CLI's OAuth token. Each brain has a Python wrapper under `lib/gateway/brains/<name>.py` that the dispatcher (`dispatch.py:_BRAIN_REGISTRY`) routes to. The triage layer consults this matrix when picking a brain — a vision-bearing image event must not be routed to a brain without vision support.

## Matrix

| Brain     | Tools         | Vision  | File edits | Web | Invocation                  | Resume mechanism                        |
|-----------|---------------|---------|-----------:|-----|-----------------------------|------------------------------------------|
| claude    | yes           | yes     | yes        | yes | `claude -p` subprocess      | `--resume <uuid>`                        |
| codex     | yes           | partial | yes        | no  | `codex exec` subprocess     | `codex exec resume <uuid>`               |
| codex_api | no (no shell) | partial | no         | no  | direct Responses API call   | API conversation id (no shell session)   |
| gemini    | partial       | yes     | partial    | yes | `gemini -p` subprocess      | `gemini --resume <uuid \| latest>`       |
| opencode  | yes           | no      | yes        | no  | `opencode run` subprocess   | `opencode run --session <id>`            |
| aider     | yes (git ops) | no      | yes        | no  | `aider` subprocess          | history file in `AIDER_HISTORY_DIR/<id>` |

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
- 2026-05-01: `codex_api` is the only brain that bypasses the shell-adapter
  contract. It is intended for triage (where launching `codex exec` per
  inbound message is too slow) and emits adapter rcs 10/11/12 so the
  recovery classifier can distinguish re-login vs. transient API failure.
  Token source is the local Codex CLI OAuth file, refreshed via
  `bin/jc-codex-auth`.
