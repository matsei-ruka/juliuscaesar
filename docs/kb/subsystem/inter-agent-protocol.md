---
title: Inter-agent protocol
section: subsystem
status: active
last_verified: 2026-05-15
verified_by: Rachel Zane
code_anchors:
  - path: lib/gateway/config.py
    symbol: InterAgentProtocolConfig
  - path: lib/gateway/context.py
    symbol: render_authority_map_block
  - path: lib/gateway/brains/claude.py
    symbol: render_authority_map_block
  - path: lib/memory/scaffolding.py
    symbol: scaffold_inter_agent
  - path: lib/health/inter_agent_check.py
    symbol: check_inter_agent
  - path: bin/jc-doctor
  - path: tests/gateway/test_context.py
    symbol: AuthorityMapBlockTests
  - path: tests/health/test_inter_agent_check.py
related:
  - subsystem/accountabilities.md
  - subsystem/entities.md
  - subsystem/adaptive-discovery.md
sources:
  - path: docs/specs/inter-agent-protocol.md
    title: Inter-agent protocol spec (P1–P7)
---

# Inter-agent protocol

## What it is

Opt-in L1 declaration of the peer-agent ecosystem this instance operates inside. The Authority Map (`memory/L1/authority-map.md`) records each peer's `agent_id`, role, human authority, accountabilities pointer, channel, and instance id. When `inter_agent_protocol.enabled: true`, the gateway injects the full map into the brain preamble under `# Inter-agent authority map`. The constitutional § section in `RULES.md` declares the five operative principles.

Shipped across phases 1–5 on branch `spec/multi-agent-awareness`. Phase 6 (peer-channel registry) deferred. Phase 7 (smoke) verified on a fresh test instance — 4/4 inter-agent checks green.

## Key invariants

1. **Feature is opt-in.** `inter_agent_protocol.enabled` defaults to `false`. When disabled, `render_authority_map_block()` returns `""`, the map is NOT injected, and `check_inter_agent()` emits a single `INFO`.
2. **Authority Map path is operator-configurable.** Default `memory/L1/authority-map.md`. `authority_map_path` in `gateway.yaml` overrides. Preamble cache fingerprint includes the default path's mtime — non-default paths are picked up via `ops/gateway.yaml` mtime invalidation.
3. **Self declaration matches a row.** `self: <agent_id>` must equal exactly one `agent_id` cell in the `## Agents` table. When `require_self_declaration: true` (default) and `self:` is missing, the doctor warns.
4. **Authority for map changes reuses the accountabilities flow.** v1 has no dedicated `inter_agent_protocol.authority_*` keys — `accountabilities.authority_channel` + `enactment_token` gate constitutional/authority changes.
5. **Identity is channel-based, not signed.** The protocol trusts that the channel listed in the map identifies the peer. There is no cryptographic attestation in v1.
6. **The framework does not transport messages between agents.** Peer interaction is operator-mediated; the protocol governs reasoning, not routing.

## Architecture

```
ops/gateway.yaml
  inter_agent_protocol:
    enabled: true
    authority_map_path: memory/L1/authority-map.md
    require_self_declaration: true
         │
         ▼
lib/gateway/config.py → InterAgentProtocolConfig (frozen dataclass)
         │
         ├── lib/gateway/context.py
         │     render_authority_map_block(instance_dir)
         │     → full frontmatter + body in render_preamble() + Claude per-event prefix
         │     _fingerprint() includes memory/L1/authority-map.md mtime
         │
         ├── lib/memory/scaffolding.py
         │     scaffold_inter_agent(instance_dir)
         │     → copies authority-map.md.template + patches CLAUDE.md import
         │     prints the constitutional § snippet for paste into RULES.md
         │
         └── lib/health/inter_agent_check.py
               check_inter_agent(instance_dir) → list[HealthItem]
               called by bin/jc-doctor "Inter-agent protocol" section
```

**File layout** (all paths relative to `instance_dir`):

```
memory/L1/authority-map.md     ← Agents table + Self declaration + Notes
memory/L1/RULES.md             ← must contain "Inter-Agent Protocol" + ≥3 of 5 principles
CLAUDE.md                      ← scaffolder patches in @memory/L1/authority-map.md import
```

## Mini recipe

**Enable on an instance:**

```
1. Run: jc memory scaffold inter-agent
     Copies authority-map.md.template and patches CLAUDE.md import (idempotent).

2. Edit memory/L1/authority-map.md:
     - Fill the Agents table with one row per peer (including this instance).
     - Set `self: <agent_id>` to this instance's row.

3. Paste the printed constitutional snippet into memory/L1/RULES.md
     under your next free §-number.

4. Flip ops/gateway.yaml:
     inter_agent_protocol:
       enabled: true

5. Run: jc-doctor
     All Inter-agent protocol items should show ✓ ok.
```

**Required Agents table columns:**
`agent_id`, `display_name`, `role`, `human_authority`, `accountabilities_pointer`, `channel`, `instance_id`. Doctor warns if any are missing from the header row.

## Gotchas

- **Pointer reachability is path-prefix sensitive.** Pointers starting with `/` (absolute) or `..` (parent-relative) are treated as cross-instance and pass with `INFO` (not validated). Pointers like `memory/L1/...` must resolve on disk from `instance_dir`, else warn.
- **`accountabilities_pointer: TBD` is explicitly allowed.** Empty string is also allowed. Anything else is treated as a path and validated.
- **HTML-comment-only rows are skipped.** The template ships with an `<!-- example: ... -->` row; the parser ignores rows whose every cell starts with `<!--` or is empty.
- **`require_self_declaration: false` only downgrades to INFO, doesn't suppress.** A missing `self:` line is still logged so operators see it.
- **CLAUDE.md patch is idempotent.** Re-running `scaffold inter-agent` does not duplicate the import; it only adds the line if absent.

## Open questions / known stale

- **2026-05-15**: P6 (dedicated peer-channel transport) deferred. Today peer interaction is operator-mediated via the principal's chat channels.
- **2026-05-15**: Identity attestation (signed tokens) tracked in spec Open questions — no key infra in v1.
- **2026-05-15**: Cross-instance authority-map sync (`jc inter-agent sync --from <peer-instance-dir>`) tracked in spec Open questions.

## See also

- `subsystem/accountabilities.md` — shares the authority-change gate
- `subsystem/entities.md` — peer rows often correspond to entity records
- `subsystem/adaptive-discovery.md` — peer identity verification per the discovery protocol
- `docs/specs/inter-agent-protocol.md` — full spec with all 7 phases
